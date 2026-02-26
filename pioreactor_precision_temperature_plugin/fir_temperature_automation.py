# -*- coding: utf-8 -*-
"""
[temperature_automation]
# MLX90632 I2C address
mlx_address=0x3A
# sensor/inference period in seconds
sample_period_s=1.0

"""
from __future__ import annotations

import math
import time
import typing as t
import uuid
from contextlib import suppress

import click
from msgspec import Meta
from msgspec.json import decode as json_decode
from msgspec.yaml import decode as yaml_decode
from pioreactor import error_codes
from pioreactor import exc
from pioreactor import hardware
from pioreactor import structs
from pioreactor import types as pt
from pioreactor.automations.base import AutomationJob
from pioreactor.automations.events import UpdatedHeaterDC
from pioreactor.calibrations.registry import CalibrationProtocol
from pioreactor.calibrations.session_flow import fields
from pioreactor.calibrations.session_flow import SessionContext
from pioreactor.calibrations.session_flow import SessionStep
from pioreactor.calibrations.session_flow import StepRegistry
from pioreactor.calibrations.session_flow import steps
from pioreactor.calibrations.structured_session import CalibrationSession
from pioreactor.calibrations.structured_session import utc_iso_timestamp
from pioreactor.config import config
from pioreactor.estimators import ESTIMATOR_PATH
from pioreactor.pubsub import post_into
from pioreactor.pubsub import subscribe
from pioreactor.utils import clamp
from pioreactor.utils import is_pio_job_running
from pioreactor.utils import local_intermittent_storage
from pioreactor.utils import local_persistent_storage
from pioreactor.utils import whoami
from pioreactor.utils.networking import resolve_to_address
from pioreactor.utils.pwm import PWM
from pioreactor.utils.streaming_calculations import PID
from pioreactor.utils.timing import current_utc_datestamp
from pioreactor.utils.timing import current_utc_datetime
from pioreactor.utils.timing import current_utc_timestamp
from pioreactor.utils.timing import RepeatedTimer
from pioreactor.whoami import get_assigned_experiment_name
from pioreactor.whoami import get_pioreactor_model
from pioreactor.whoami import get_testing_experiment_name
from pioreactor.whoami import get_unit_name


__plugin_name__ = "fir-temperature-automation"
__plugin_author__ = "Pioreactor"
__plugin_summary__ = (
    "Replaces temperature automation with FIR MLX/ambient/PCB estimation and thermostat bias trim."
)
__plugin_version__ = "0.1.0"


TEMPERATURE_FIR_DEVICE = "temperature_fir"

STABILIZATION_TARGET_ERROR_C = 0.1
STABILIZATION_MAX_SLOPE_C_PER_MIN = 0.02
STABILIZATION_WINDOW_S = 2.0 * 60
STABILIZATION_SLOPE_SAMPLE_DELAY_S = 4.1
STABILIZATION_TIMEOUT_S = 90 * 60

available_temperature_automations: dict[str, type["TemperatureAutomationJobFIR"]] = {}


class classproperty(property):
    def __get__(self, obj, objtype=None):
        return self.fget(objtype)


class FIRTemperatureEstimator(structs.EstimatorBase, kw_only=True, tag="fir_temperature_estimator"):

    w_obj: float
    w_amb: float
    w_pcb: float
    c: float
    tau_s: t.Annotated[float, Meta(gt=0)]
    k_vol: float = 0.0
    volume_ref_ml: t.Annotated[float, Meta(gt=0)] = 25.0

    user_bias_c: float = 0.0

    y: str = "temperature_c"


class _MockMLX90632:
    """Simple MLX stand-in for TESTING environments."""

    data_ready = True

    @property
    def object_temperature(self) -> float:
        return 25.0

    @property
    def ambient_temperature(self) -> float:
        return 24.0

    def reset_data_ready(self) -> None:
        return


class TemperatureAutomationJobFIR(AutomationJob):
    automation_name = "temperature_automation_base"
    job_name = "temperature_automation"

    published_settings: dict[str, pt.PublishableSetting] = {}

    latest_temperature = None
    previous_temperature = None

    @classproperty
    def MAX_TEMP_TO_REDUCE_HEATING(cls) -> float:
        return get_pioreactor_model().max_temp_to_reduce_heating

    @classproperty
    def MAX_TEMP_TO_DISABLE_HEATING(cls) -> float:
        return get_pioreactor_model().max_temp_to_disable_heating

    @classproperty
    def MAX_TEMP_TO_SHUTDOWN(cls) -> float:
        return get_pioreactor_model().max_temp_to_shutdown

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if (
            hasattr(cls, "automation_name")
            and getattr(cls, "automation_name") != "temperature_automation_base"
        ):
            available_temperature_automations[cls.automation_name] = cls

    def __init__(
        self,
        unit: str,
        experiment: str,
        **kwargs,
    ) -> None:
        super().__init__(unit, experiment)

        self.add_to_published_settings(
            "temperature", {"datatype": "Temperature", "settable": False, "unit": "℃"}
        )

        self.add_to_published_settings(
            "heater_duty_cycle",
            {"datatype": "float", "settable": False, "unit": "%"},
        )

        if not hardware.is_heating_pcb_present():
            self.logger.error("Heating PCB must be attached to Pioreactor HAT")
            self.clean_up()
            raise exc.HardwareNotFoundError("Heating PCB must be attached to Pioreactor HAT")

        self._sample_period_s = config.getfloat("temperature_automation", "sample_period_s", fallback=1.0)
        self._mlx_address = int(config.get("temperature_automation", "mlx_address", fallback="0x3A"), 16)

        self.heater_duty_cycle = 0.0
        self.pwm = self.setup_pwm()

        self.heating_pcb_tmp_driver = self._setup_heating_pcb_tmp_sensor()
        self.mlx_driver = self._setup_mlx_driver(self._mlx_address)
        self.estimator = self._load_active_fir_estimator()
        self._volume_ml = config.getfloat("bioreactor", "initial_volume_ml", fallback=25.0)
        self._liquid_temperature_estimate: float | None = None
        self._last_sample_monotonic: float | None = None

        self.subscribe_and_callback(
            self._update_volume_from_mqtt,
            f"pioreactor/{self.unit}/{self.experiment}/dosing_automation/current_volume_ml",
            allow_retained=True,
        )

        self.read_external_temperature_timer = RepeatedTimer(
            20,
            self.read_heating_pcb_temperature,
            job_name=self.job_name,
            run_immediately=False,
            logger=self.logger,
        ).start()

        self.publish_temperature_timer = RepeatedTimer(
            max(0.25, float(self._sample_period_s)),
            self.infer_temperature,
            job_name=self.job_name,
            run_immediately=True,
            logger=self.logger,
        ).start()

        self.latest_temperature_at = current_utc_datetime()

    def on_init_to_ready(self):
        self.infer_temperature()

    def on_disconnected(self) -> None:
        with suppress(AttributeError):
            self.publish_temperature_timer.cancel()
            self.read_external_temperature_timer.cancel()

        with suppress(AttributeError):
            self.turn_off_heater()

    def on_sleeping(self) -> None:
        self.publish_temperature_timer.pause()
        self._update_heater(0)

    def on_sleeping_to_ready(self) -> None:
        self.publish_temperature_timer.unpause()

    def _setup_heating_pcb_tmp_sensor(self):
        if whoami.is_testing_env():
            from pioreactor.utils.mock import MockTMP1075 as TMP1075
        else:
            from pioreactor.utils.temps import TMP1075  # type: ignore
        return TMP1075(address=hardware.get_temp_address())

    def _setup_mlx_driver(self, address: int):
        if whoami.is_testing_env():
            return _MockMLX90632()

        try:
            import adafruit_mlx90632  # type: ignore
            import board  # type: ignore
        except Exception as e:
            raise exc.HardwareNotFoundError(f"Unable to import MLX90632 dependencies: {e}")

        i2c = board.I2C()
        sensor = adafruit_mlx90632.MLX90632(i2c, address=address)
        with suppress(Exception):
            sensor.mode = adafruit_mlx90632.MODE_CONTINUOUS
            sensor.refresh_rate = adafruit_mlx90632.REFRESH_1HZ
        return sensor

    def _load_active_fir_estimator(self) -> FIRTemperatureEstimator:
        with local_persistent_storage("active_estimators") as storage:
            active_name = storage.get(TEMPERATURE_FIR_DEVICE)

        if active_name is None:
            raise ValueError("Estimator required to be active")

        estimator_path = ESTIMATOR_PATH / TEMPERATURE_FIR_DEVICE / f"{active_name}.yaml"
        if not estimator_path.exists():
            raise FileNotFoundError(
                f"No FIR estimator found at {estimator_path}. Copy the estimator YAML and set it active first."
            )

        return yaml_decode(estimator_path.read_bytes(), type=FIRTemperatureEstimator)

    def _update_volume_from_mqtt(self, message: pt.MQTTMessage) -> None:
        if not message.payload:
            return

        try:
            self._volume_ml = float(message.payload)
        except (TypeError, ValueError):
            self.logger.debug("Ignoring invalid current_volume_ml payload: %r", message.payload)

    def setup_pwm(self) -> PWM:
        heater_channel = hardware.get_heater_pwm_channel()
        pin = hardware.get_pwm_to_pin_map()[heater_channel]

        pwm = PWM(
            pin,
            16,
            unit=self.unit,
            experiment=self.experiment,
            pub_client=self.pub_client,
            logger=self.logger,
        )
        pwm.start(0)
        return pwm

    def turn_off_heater(self) -> None:
        self._update_heater(0)
        self.pwm.clean_up()

        pwm = self.setup_pwm()
        pwm.change_duty_cycle(0)
        pwm.clean_up()

    def is_heater_pwm_locked(self) -> bool:
        return self.pwm.is_locked()

    def update_heater(self, new_duty_cycle: float) -> bool:
        if not self.pwm.is_locked():
            return self._update_heater(new_duty_cycle)
        return False

    def update_heater_with_delta(self, delta_duty_cycle: float) -> bool:
        return self.update_heater(self.heater_duty_cycle + delta_duty_cycle)

    def _update_heater(self, new_duty_cycle: float) -> bool:
        self.heater_duty_cycle = clamp(0.0, round(float(new_duty_cycle), 2), 100.0)
        self.pwm.change_duty_cycle(self.heater_duty_cycle)

        if self.heater_duty_cycle == 0.0:
            with local_intermittent_storage("temperature_and_heating") as cache:
                cache["last_heating_timestamp"] = current_utc_timestamp()

        return True

    def _read_heating_pcb_temperature(self) -> float:
        samples = 5
        running_sum = 0.0

        try:
            for _ in range(samples):
                running_sum += self.heating_pcb_tmp_driver.get_temperature()
                time.sleep(0.05)
        except OSError as e:
            self.logger.debug(e, exc_info=True)
            raise exc.HardwareNotFoundError(
                "Is the Heating PCB attached to the Pioreactor HAT? Unable to find temperature sensor."
            )

        averaged_temp = running_sum / samples
        with local_intermittent_storage("temperature_and_heating") as cache:
            cache["heating_pcb_temperature"] = averaged_temp
            cache["heating_pcb_temperature_at"] = current_utc_timestamp()

        return averaged_temp

    def _check_if_exceeds_max_temp(self, temp: float) -> float:
        if temp > self.MAX_TEMP_TO_SHUTDOWN:
            self.logger.error(
                f"Temperature of heating surface exceeded {self.MAX_TEMP_TO_SHUTDOWN}℃ (currently {temp}℃). Shutting down Raspberry Pi to prevent damage."
            )
            self._update_heater(0)
            self.blink_error_code(error_codes.PCB_TEMPERATURE_TOO_HIGH)
            from subprocess import call

            call("sudo shutdown now --poweroff", shell=True)

        elif temp > self.MAX_TEMP_TO_DISABLE_HEATING:
            self.blink_error_code(error_codes.PCB_TEMPERATURE_TOO_HIGH)
            self.logger.warning(
                f"Temperature of heating surface exceeded {self.MAX_TEMP_TO_DISABLE_HEATING}℃ (currently {temp}℃). Forcing heater to 0%."
            )
            self._update_heater(0)

        elif temp > self.MAX_TEMP_TO_REDUCE_HEATING:
            self.logger.debug(
                f"Temperature of heating surface exceeded {self.MAX_TEMP_TO_REDUCE_HEATING}℃ (currently {temp}℃). Reducing heater by 10%."
            )
            self._update_heater(self.heater_duty_cycle * 0.9)

        return temp

    def read_heating_pcb_temperature(self) -> float:
        return self._check_if_exceeds_max_temp(self._read_heating_pcb_temperature())

    def _read_mlx(self) -> tuple[float, float] | None:
        if hasattr(self.mlx_driver, "data_ready") and not self.mlx_driver.data_ready:
            return None

        obj_temp = float(self.mlx_driver.object_temperature)
        amb_temp = float(self.mlx_driver.ambient_temperature)

        with suppress(Exception):
            self.mlx_driver.reset_data_ready()

        if not (math.isfinite(obj_temp) and math.isfinite(amb_temp)):
            return None

        return obj_temp, amb_temp

    def _compute_unlagged_temperature(
        self, mlx_object_temp: float, mlx_ambient_temp: float, pcb_temp: float
    ) -> float:
        est = self.estimator
        volume_correction = est.k_vol * (self._volume_ml - est.volume_ref_ml)
        return (
            est.w_obj * mlx_object_temp
            + est.w_amb * mlx_ambient_temp
            + est.w_pcb * pcb_temp
            + est.c
            + volume_correction
            + est.user_bias_c
        )

    def _apply_lag(self, unlagged_temp: float) -> float:
        now = time.monotonic()

        if self._liquid_temperature_estimate is None or self._last_sample_monotonic is None:
            self._liquid_temperature_estimate = unlagged_temp
            self._last_sample_monotonic = now
            return unlagged_temp

        dt = max(0.001, now - self._last_sample_monotonic)
        tau_s = max(0.001, self.estimator.tau_s)
        alpha = 1.0 - math.exp(-dt / tau_s)

        self._liquid_temperature_estimate = self._liquid_temperature_estimate + alpha * (
            unlagged_temp - self._liquid_temperature_estimate
        )
        self._last_sample_monotonic = now

        if not math.isfinite(self._liquid_temperature_estimate):
            self._liquid_temperature_estimate = unlagged_temp

        return self._liquid_temperature_estimate

    def infer_temperature(self) -> None:
        reading = self._read_mlx()
        if reading is None:
            return

        try:
            pcb_temp = self.read_heating_pcb_temperature()
        except exc.HardwareNotFoundError:
            return

        mlx_object_temp, mlx_ambient_temp = reading

        try:
            unlagged = self._compute_unlagged_temperature(mlx_object_temp, mlx_ambient_temp, pcb_temp)
            inferred_temperature = self._apply_lag(unlagged)

            self.temperature = structs.Temperature(
                temperature=round(inferred_temperature, 3),
                timestamp=current_utc_datetime(),
            )
            self._set_latest_temperature(self.temperature)
        except Exception as e:
            self.logger.debug(e, exc_info=True)
            self.logger.error(e)

    def _set_latest_temperature(self, temperature: structs.Temperature) -> None:
        self.previous_temperature = self.latest_temperature
        self.latest_temperature = temperature.temperature
        self.latest_temperature_at = temperature.timestamp

        if self.state == self.READY or self.state == self.INIT:
            self.latest_event = self.execute()

    def execute(self):
        return None


class Thermostat(TemperatureAutomationJobFIR):
    automation_name = "thermostat"
    published_settings = {
        "target_temperature": {"datatype": "float", "unit": "℃", "settable": True},
    }

    @classproperty
    def MAX_TARGET_TEMP(cls) -> float:
        return get_pioreactor_model().max_temp_to_reduce_heating

    def __init__(self, target_temperature: float | str, **kwargs) -> None:
        super().__init__(**kwargs)

        self.pid = PID(
            Kp=config.getfloat("temperature_automation.thermostat", "Kp"),
            Ki=config.getfloat("temperature_automation.thermostat", "Ki"),
            Kd=config.getfloat("temperature_automation.thermostat", "Kd"),
            setpoint=None,
            unit=self.unit,
            experiment=self.experiment,
            job_name=self.job_name,
            target_name="temperature",
            output_limits=(-2.5, 2.5),
            derivative_smoothing=0.925,
            pub_client=self.pub_client,
        )

        self.set_target_temperature(target_temperature)

    def on_init_to_ready(self):
        super().on_init_to_ready()
        if not is_pio_job_running("stirring"):
            self.logger.warning("It's recommended to have stirring on when using the thermostat.")

    def on_disconnected(self) -> None:
        super().on_disconnected()
        with suppress(AttributeError):
            self.pid.clean_up()

    def _clamp_target_temperature(self, target_temperature: float) -> float:
        if target_temperature > self.MAX_TARGET_TEMP:
            self.logger.warning(
                f"Values over {self.MAX_TARGET_TEMP}℃ are not supported. Setting to {self.MAX_TARGET_TEMP}℃."
            )
        return clamp(0.0, target_temperature, self.MAX_TARGET_TEMP)

    def set_target_temperature(self, target_temperature: float | str) -> None:
        target_temperature = float(target_temperature)
        self.target_temperature = self._clamp_target_temperature(target_temperature)
        self.pid.set_setpoint(self.target_temperature)

    def execute(self):
        while not hasattr(self, "pid") and self.state is self.READY:
            pass

        assert self.latest_temperature is not None
        output = self.pid.update(self.latest_temperature, dt=self._sample_period_s)
        self.update_heater_with_delta(output)

        return UpdatedHeaterDC(
            f"delta_dc={output}",
            data={
                "current_dc": self.heater_duty_cycle,
                "delta_dc": output,
                "target_temperature": self.target_temperature,
                "latest_temperature": self.latest_temperature,
            },
        )


class OnlyRecordTemperature(TemperatureAutomationJobFIR):
    automation_name = "only_record_temperature"

    def execute(self):
        return None


def start_temperature_automation(
    automation_name: str,
    unit: str | None = None,
    experiment: str | None = None,
    **kwargs,
) -> TemperatureAutomationJobFIR:
    unit = unit or get_unit_name()
    experiment = experiment or get_assigned_experiment_name(unit)

    try:
        klass = available_temperature_automations[automation_name]
    except KeyError:
        raise KeyError(
            f"Unable to find {automation_name}. Available automations are {list(available_temperature_automations.keys())}"
        )

    if "skip_first_run" in kwargs:
        del kwargs["skip_first_run"]

    return klass(
        unit=unit,
        experiment=experiment,
        automation_name=automation_name,
        **kwargs,
    )


@click.command(
    name="temperature_automation",
    context_settings=dict(ignore_unknown_options=True, allow_extra_args=True),
)
@click.option(
    "--automation-name",
    help="set the automation of the system: thermostat, etc.",
    required=True,
)
@click.pass_context
def click_temperature_automation(ctx, automation_name):
    with start_temperature_automation(
        automation_name=automation_name,
        **{ctx.args[i][2:].replace("-", "_"): ctx.args[i + 1] for i in range(0, len(ctx.args), 2)},
    ) as ta:
        ta.block_until_disconnected()


def _load_active_fir_estimator() -> FIRTemperatureEstimator:
    with local_persistent_storage("active_estimators") as storage:
        estimator_name = storage.get(TEMPERATURE_FIR_DEVICE)

    if estimator_name is None:
        raise ValueError("Estimator required to be active")

    estimator_path = ESTIMATOR_PATH / TEMPERATURE_FIR_DEVICE / f"{estimator_name}.yaml"
    if not estimator_path.exists():
        raise FileNotFoundError(
            f"No active FIR estimator found at {estimator_path}. Copy the base estimator YAML and set it active."
        )

    return yaml_decode(estimator_path.read_bytes(), type=FIRTemperatureEstimator)


def _unit_api_address() -> str:
    return resolve_to_address(get_unit_name())


def _start_bias_trim_heating(session_id: str, target_temperature: float) -> str:
    experiment = get_testing_experiment_name()
    job_source = f"fir_bias_trim_{session_id}"

    payload = {
        "args": [],
        "options": {
            "automation_name": "thermostat",
            "target_temperature": target_temperature,
        },
        "env": {
            "EXPERIMENT": experiment,
            "JOB_SOURCE": job_source,
        },
        "config_overrides": [],
    }

    response = post_into(
        _unit_api_address(),
        "/unit_api/jobs/run/job_name/temperature_automation",
        json=payload,
        timeout=8,
    )

    if not response.ok:
        raise RuntimeError(f"Unable to start temperature automation: {response.status_code}")

    return job_source


def _start_bias_trim_stirring(session_id: str) -> str:
    experiment = get_testing_experiment_name()
    job_source = f"fir_bias_trim_{session_id}"

    payload = {
        "args": [],
        "options": {},
        "env": {
            "EXPERIMENT": experiment,
            "JOB_SOURCE": job_source,
        },
        "config_overrides": [],
    }

    response = post_into(
        _unit_api_address(),
        "/unit_api/jobs/run/job_name/stirring",
        json=payload,
        timeout=8,
    )

    if not response.ok:
        raise RuntimeError(f"Unable to start stirring: {response.status_code}")

    return job_source


def _stop_job_by_source(job_source: str) -> None:
    with suppress(Exception):
        post_into(
            _unit_api_address(),
            "/unit_api/jobs/stop",
            json={"job_source": job_source},
            timeout=8,
        )


def _stop_bias_trim_heating(job_source: str) -> None:
    _stop_job_by_source(job_source)


def _stop_bias_trim_stirring(job_source: str) -> None:
    _stop_job_by_source(job_source)


def _stop_bias_trim_jobs(data: t.Mapping[str, t.Any]) -> None:
    job_source = data.get("job_source")
    if job_source is None:
        return
    assert isinstance(job_source, str)
    _stop_bias_trim_heating(job_source)
    _stop_bias_trim_stirring(job_source)


def _fetch_current_temperature(unit: str, experiment: str) -> structs.Temperature | None:
    topic = f"pioreactor/{unit}/{experiment}/temperature_automation/temperature"
    message = subscribe(topic, timeout=5.0, allow_retained=True)
    if message is None or not message.payload:
        return None

    return json_decode(message.payload, type=structs.Temperature)


def _now_ts() -> float:
    return current_utc_datetime().timestamp()


def _windowed_history(history: list[dict[str, float]]) -> list[dict[str, float]]:
    if not history:
        return []

    newest = history[-1]["ts"]
    min_ts = newest - STABILIZATION_WINDOW_S
    return [row for row in history if row["ts"] >= min_ts]


def _compute_window_slope_c_per_min(history_window: list[dict[str, float]]) -> float:
    if len(history_window) < 2:
        return float("inf")

    dt_s = history_window[-1]["ts"] - history_window[0]["ts"]
    if dt_s <= 0:
        return float("inf")

    dt_min = dt_s / 60.0
    return (history_window[-1]["temperature"] - history_window[0]["temperature"]) / dt_min


def _update_stabilization_state(
    ctx: SessionContext, measured_temperature: float, target_temperature: float
) -> None:
    now_ts = _now_ts()
    history = ctx.data.get("stability_history", [])
    if not isinstance(history, list):
        history = []

    history.append(
        {
            "ts": now_ts,
            "temperature": measured_temperature,
            "error_abs": abs(measured_temperature - target_temperature),
        }
    )

    # Keep up to 3h of data max.
    earliest = now_ts - 3 * 3600
    history = [row for row in history if row.get("ts", 0) >= earliest]

    latest_abs_error = abs(measured_temperature - target_temperature)
    error_ok = latest_abs_error <= STABILIZATION_TARGET_ERROR_C
    slope_c_per_min = _compute_window_slope_c_per_min(history[-2:])
    has_slope_sample = len(history) >= 2 and math.isfinite(slope_c_per_min)
    slope_ok = has_slope_sample and (abs(slope_c_per_min) <= STABILIZATION_MAX_SLOPE_C_PER_MIN)
    is_stable = error_ok and slope_ok

    ctx.data["stability_history"] = history
    ctx.data["latest_estimated_temperature"] = measured_temperature
    ctx.data["latest_abs_error"] = latest_abs_error
    ctx.data["latest_slope_c_per_min"] = slope_c_per_min
    ctx.data["stabilization_error_ok"] = error_ok
    ctx.data["stabilization_slope_ok"] = slope_ok
    ctx.data["stabilization_has_slope_sample"] = has_slope_sample
    ctx.data.pop("stabilization_span_ok", None)
    ctx.data.pop("stabilization_window_span_s", None)
    ctx.data.pop("stabilization_required_span_s", None)
    ctx.data.pop("stabilization_stable_since_ts", None)
    ctx.data["is_stable"] = is_stable


def _mark_failed_and_stop(ctx: SessionContext, error_message: str) -> None:
    _stop_bias_trim_jobs(ctx.data)
    ctx.session.status = "failed"
    ctx.session.error = error_message
    ctx.session.step_id = "ended"


def _default_bias_trim_name(base_estimator_name: str) -> str:
    return f"{base_estimator_name}-bias-trim-{current_utc_datestamp()}"


def start_fir_temperature_bias_trim_session(target_device: str) -> CalibrationSession:
    if target_device != TEMPERATURE_FIR_DEVICE:
        raise ValueError(f"Target device must be {TEMPERATURE_FIR_DEVICE}.")

    session_id = str(uuid.uuid4())
    now = utc_iso_timestamp()

    return CalibrationSession(
        session_id=session_id,
        protocol_name=FIRTemperatureBiasTrimProtocol.protocol_name,
        target_device=target_device,
        status="in_progress",
        step_id=BiasTrimIntroStep.step_id,
        data={
            "unit": get_unit_name(),
            "experiment": get_testing_experiment_name(),
            "started_at_ts": _now_ts(),
        },
        created_at=now,
        updated_at=now,
    )


class BiasTrimIntroStep(SessionStep):
    step_id = "intro"

    def render(self, ctx: SessionContext):
        return steps.info(
            "FIR temperature bias trim",
            "This flow runs thermostat control to a target temperature, waits for stabilization, and then computes a single-point user bias from your probe reading.",
        )

    def advance(self, ctx: SessionContext):
        return BiasTrimTargetStep()


class BiasTrimTargetStep(SessionStep):
    step_id = "target"

    def render(self, ctx: SessionContext):
        default_target = float(ctx.data.get("target_temperature", 37.0))
        max_target = get_pioreactor_model().max_temp_to_reduce_heating
        return steps.form(
            "Target temperature",
            "Use the same liquid volume as your experiment, then enter the target temperature to center the bias trim around.",
            [
                fields.float(
                    "target_temperature",
                    label="Target temperature (℃)",
                    default=default_target,
                    minimum=0.0,
                    maximum=max_target,
                )
            ],
        )

    def advance(self, ctx: SessionContext):
        target_temperature = ctx.inputs.float("target_temperature")
        ctx.data["target_temperature"] = target_temperature

        if "job_source" not in ctx.data:
            try:
                ctx.data["job_source"] = _start_bias_trim_stirring(ctx.session.session_id)
            except Exception as e:
                _mark_failed_and_stop(ctx, f"Unable to start stirring for bias trim: {e}")
                return None

            try:
                js = _start_bias_trim_heating(ctx.session.session_id, target_temperature)
                assert ctx.data["job_source"] == js
                ctx.data["stabilization_started_at_ts"] = _now_ts()
            except Exception as e:
                _mark_failed_and_stop(ctx, f"Unable to start thermostat for bias trim: {e}")
                return None

        return BiasTrimStabilizeStep()


class BiasTrimStabilizeStep(SessionStep):
    step_id = "stabilize"

    def render(self, ctx: SessionContext):
        target_temperature = float(ctx.data.get("target_temperature", 37.0))
        latest = ctx.data.get("latest_estimated_temperature")
        latest_error = ctx.data.get("latest_abs_error")
        latest_slope = ctx.data.get("latest_slope_c_per_min")
        error_ok = ctx.data.get("stabilization_error_ok")
        slope_ok = ctx.data.get("stabilization_slope_ok")
        has_slope_sample = ctx.data.get("stabilization_has_slope_sample")

        status_lines = [
            f"Waiting for stabilization near {target_temperature:.2f}℃. (absolute error ≤ {STABILIZATION_TARGET_ERROR_C}, and rate of change ≤ {STABILIZATION_SLOPE_SAMPLE_DELAY_S}) \n",
        ]

        if isinstance(latest, (int, float)):
            status_lines.append(f"Latest estimate: {float(latest):.2f}℃")
        if isinstance(latest_error, (int, float)):
            status_lines.append(f"Absolute error: {float(latest_error):.2f}℃")
        if isinstance(latest_slope, (int, float)) and math.isfinite(float(latest_slope)):
            status_lines.append(f"Rate of change: {float(latest_slope):.2f}℃/min")

        status_lines.append("\nPress Continue to run a stabilization check.")

        step = steps.action("Stabilizing...", "\n".join(status_lines))

        step.metadata = {
            "image": {
                "src": "/static/svgs/vial-stirbar-heated.svg",
                "alt": "Stirring and heating.",
                "caption": "Stirring and heating started, waiting for stabilization.",
            }
        }

        return step

    def _check_once(self, ctx: SessionContext) -> bool:
        unit = str(ctx.data["unit"])
        experiment = str(ctx.data["experiment"])
        target = float(ctx.data["target_temperature"])

        reading = _fetch_current_temperature(unit, experiment)
        if reading is None:
            return False

        _update_stabilization_state(ctx, reading.temperature, target)
        time.sleep(STABILIZATION_SLOPE_SAMPLE_DELAY_S)
        follow_up = _fetch_current_temperature(unit, experiment)
        if follow_up is not None:
            _update_stabilization_state(ctx, follow_up.temperature, target)

        if bool(ctx.data.get("is_stable", False)):
            estimate_reading = follow_up if follow_up is not None else reading
            ctx.data["stable_temperature_estimate"] = estimate_reading.temperature
            return True

        started = float(ctx.data.get("stabilization_started_at_ts", _now_ts()))
        if (_now_ts() - started) > STABILIZATION_TIMEOUT_S:
            _mark_failed_and_stop(
                ctx,
                "Timed out waiting for stabilization (90 minutes). Consider adjusting target temperature and retrying.",
            )
            return False

        return False

    def advance(self, ctx: SessionContext):

        if self._check_once(ctx):
            return BiasTrimProbeStep()
        return BiasTrimStabilizeStep()


class BiasTrimProbeStep(SessionStep):
    step_id = "probe"

    def render(self, ctx: SessionContext):
        estimated = float(
            ctx.data.get("stable_temperature_estimate", ctx.data.get("latest_estimated_temperature", 0.0))
        )
        return steps.form(
            "Enter measured temperature",
            (
                f"Estimated stabilized temperature is {estimated:.3f}℃. "
                "Measure liquid temperature with an external probe, then enter that value."
            ),
            [fields.float("measured_temperature", label="Measured temperature (℃)")],
        )

    def advance(self, ctx: SessionContext):
        measured_temperature = ctx.inputs.float("measured_temperature")
        estimated_temperature = float(
            ctx.data.get("stable_temperature_estimate", ctx.data.get("latest_estimated_temperature", 0.0))
        )

        try:
            base = _load_active_fir_estimator()
        except Exception as e:
            _mark_failed_and_stop(ctx, f"Unable to load active FIR estimator: {e}")
            return None

        user_bias_c = measured_temperature - estimated_temperature
        new_name = _default_bias_trim_name(base.estimator_name)

        adjusted = FIRTemperatureEstimator(
            estimator_name=new_name,
            calibrated_on_pioreactor_unit=get_unit_name(),
            created_at=current_utc_datetime(),
            w_obj=base.w_obj,
            w_amb=base.w_amb,
            w_pcb=base.w_pcb,
            c=base.c,
            tau_s=base.tau_s,
            k_vol=base.k_vol,
            volume_ref_ml=base.volume_ref_ml,
            user_bias_c=user_bias_c,
            y=base.y,
        )

        try:
            save_result = ctx.store_estimator(adjusted, TEMPERATURE_FIR_DEVICE)
            saved_path = save_result["path"]
            if not isinstance(saved_path, str):
                raise ValueError("Estimator save did not return a path.")
            path = saved_path
        except Exception as e:
            _mark_failed_and_stop(ctx, f"Unable to save adjusted estimator: {e}")
            return None
        finally:
            _stop_bias_trim_jobs(ctx.data)

        ctx.complete(
            {
                "title": "FIR bias trim saved.",
                "device": TEMPERATURE_FIR_DEVICE,
                "base_estimator_name": base.estimator_name,
                "estimator_name": adjusted.estimator_name,
                "saved_path": path,
                "measured_temperature": measured_temperature,
                "estimated_temperature": estimated_temperature,
                "user_bias_c": user_bias_c,
            }
        )
        return None


_FIR_BIAS_TRIM_STEPS: StepRegistry = {
    BiasTrimIntroStep.step_id: BiasTrimIntroStep,
    BiasTrimTargetStep.step_id: BiasTrimTargetStep,
    BiasTrimStabilizeStep.step_id: BiasTrimStabilizeStep,
    BiasTrimProbeStep.step_id: BiasTrimProbeStep,
}


class FIRTemperatureBiasTrimProtocol(CalibrationProtocol[str]):
    protocol_name = "fir_temperature_bias_trim"
    target_device = [TEMPERATURE_FIR_DEVICE]
    title = "FIR temperature one-point bias trim"
    description = "Heat to a target, stabilize, then trim a single bias against an external probe."
    requirements = (
        "Plugin pioreactor_precision_temperature_plugin installed.",
        "External temperature probe for the final measurement.",
        "Vial with water or media, with a stir bar.",
    )
    step_registry = _FIR_BIAS_TRIM_STEPS
    priority = 2

    @classmethod
    def start_session(cls, target_device: str) -> CalibrationSession:
        return start_fir_temperature_bias_trim_session(target_device)

    def run(self, target_device: str):
        raise ValueError("Use the session flow (UI) for fir_temperature_bias_trim.")

    @classmethod
    def on_session_abort(
        cls,
        session: CalibrationSession,
        executor=None,
    ) -> None:
        data = session.data if isinstance(session.data, dict) else {}
        _stop_bias_trim_jobs(data)
