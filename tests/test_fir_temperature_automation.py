# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import sys
from threading import RLock
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from pioreactor_precision_temperature_plugin import fir_temperature_automation as fta


def test_compute_unlagged_temperature_includes_volume_and_bias() -> None:
    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    job.estimator = SimpleNamespace(
        w_obj=0.6,
        w_amb=0.2,
        w_pcb=0.1,
        c=1.5,
        k_vol=0.05,
        volume_ref_ml=25.0,
        user_bias_c=-0.3,
    )
    job.experiment = "test_compute_unlagged_temperature_includes_volume_and_bias"
    fta.bioreactor.set_bioreactor_value(job.experiment, "current_volume_ml", 30.0)

    result = job._compute_unlagged_temperature(mlx_object_temp=40.0, mlx_ambient_temp=20.0, pcb_temp=30.0)

    expected = 0.6 * 40.0 + 0.2 * 20.0 + 0.1 * 30.0 + 1.5 + 0.05 * (30.0 - 25.0) - 0.3
    assert result == pytest.approx(expected)


def test_apply_lag_initializes_and_smooths(monkeypatch: pytest.MonkeyPatch) -> None:
    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    job.estimator = SimpleNamespace(tau_s=5.0)
    job._liquid_temperature_estimate = None
    job._last_sample_monotonic = None

    monotonic_values = iter([100.0, 103.0])
    monkeypatch.setattr(fta.time, "monotonic", lambda: next(monotonic_values))

    first = job._apply_lag(30.0)
    second = job._apply_lag(40.0)

    expected_alpha = 1.0 - math.exp(-(3.0 / 5.0))
    expected_second = 30.0 + expected_alpha * (40.0 - 30.0)

    assert first == pytest.approx(30.0)
    assert second == pytest.approx(expected_second)


def test_read_mlx_returns_none_when_not_ready() -> None:
    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    job.mlx_driver = SimpleNamespace(data_ready=False, object_temperature=30.0, ambient_temperature=24.0)

    assert job._read_mlx() is None


def test_read_mlx_resets_ready_flag_and_filters_non_finite() -> None:
    class Driver:
        def __init__(self, object_temperature: float, ambient_temperature: float) -> None:
            self.data_ready = True
            self.object_temperature = object_temperature
            self.ambient_temperature = ambient_temperature
            self.reset_calls = 0

        def reset_data_ready(self) -> None:
            self.reset_calls += 1

    good_driver = Driver(object_temperature=31.2, ambient_temperature=26.4)
    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    job.mlx_driver = good_driver

    assert job._read_mlx() == (31.2, 26.4)
    assert good_driver.reset_calls == 1

    bad_driver = Driver(object_temperature=float("nan"), ambient_temperature=26.4)
    job_bad: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    job_bad.mlx_driver = bad_driver

    assert job_bad._read_mlx() is None
    assert bad_driver.reset_calls == 1


def test_setup_mlx_driver_uses_busio_i2c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pioreactor_precision_temperature_plugin as plugin_pkg

    busio_calls: list[tuple[int, int]] = []

    class FakeBusIOModule:
        @staticmethod
        def I2C(scl: int, sda: int) -> object:
            busio_calls.append((scl, sda))
            return "busio-i2c"

    created: list[tuple[object, int]] = []

    class FakeMLXModule:
        MODE_CONTINUOUS = 3
        REFRESH_1HZ = 1

        class MLX90632:
            def __init__(self, i2c_bus: object, address: int) -> None:
                created.append((i2c_bus, address))
                self.mode = None
                self.refresh_rate = None

    monkeypatch.setattr(fta.whoami, "is_testing_env", lambda: False)
    monkeypatch.setattr(fta.hardware, "get_scl_pin", lambda: 11)
    monkeypatch.setattr(fta.hardware, "get_sda_pin", lambda: 13)
    monkeypatch.setattr(fta.hardware, "is_i2c_device_present", lambda _address: True)
    monkeypatch.setitem(sys.modules, "busio", FakeBusIOModule())
    monkeypatch.setattr(plugin_pkg, "adafruit_mlx90632", FakeMLXModule, raising=False)

    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    sensor = job._setup_mlx_driver(address=0x3A)

    assert busio_calls == [(11, 13)]
    assert created == [("busio-i2c", 0x3A)]
    assert sensor.mode == FakeMLXModule.MODE_CONTINUOUS
    assert sensor.refresh_rate == FakeMLXModule.REFRESH_1HZ


def test_setup_mlx_driver_raises_clear_error_when_address_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pioreactor_precision_temperature_plugin as plugin_pkg
    from pioreactor import exc as pr_exc

    class FakeBusIOModule:
        @staticmethod
        def I2C(_scl: int, _sda: int) -> object:
            return object()

    class FakeMLXModule:
        MODE_CONTINUOUS = 3
        REFRESH_1HZ = 1

        class MLX90632:
            def __init__(self, _i2c_bus: object, _address: int) -> None:
                raise AssertionError("should not attempt MLX init when address is missing")

    monkeypatch.setattr(fta.whoami, "is_testing_env", lambda: False)
    monkeypatch.setattr(fta.hardware, "get_scl_pin", lambda: 11)
    monkeypatch.setattr(fta.hardware, "get_sda_pin", lambda: 13)
    monkeypatch.setattr(fta.hardware, "is_i2c_device_present", lambda _address: False)
    monkeypatch.setitem(sys.modules, "busio", FakeBusIOModule())
    monkeypatch.setattr(plugin_pkg, "adafruit_mlx90632", FakeMLXModule, raising=False)

    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)

    with pytest.raises(pr_exc.HardwareNotFoundError) as e:
        job._setup_mlx_driver(address=0x3A)

    assert "MLX90632 not found at address 0x3A" in str(e.value)


def test_compute_unlagged_temperature_uses_current_persisted_volume() -> None:
    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    job.experiment = "test_compute_unlagged_temperature_uses_current_persisted_volume"
    job.estimator = SimpleNamespace(
        w_obj=1.0,
        w_amb=0.0,
        w_pcb=0.0,
        c=0.0,
        k_vol=0.1,
        volume_ref_ml=25.0,
        user_bias_c=0.0,
    )

    fta.bioreactor.set_bioreactor_value(job.experiment, "current_volume_ml", 30.0)
    assert job._compute_unlagged_temperature(37.0, 24.0, 25.0) == pytest.approx(37.5)

    fta.bioreactor.set_bioreactor_value(job.experiment, "current_volume_ml", 20.0)
    assert job._compute_unlagged_temperature(37.0, 24.0, 25.0) == pytest.approx(36.5)


def trim_context() -> Any:
    return SimpleNamespace(
        data={
            "unit": "unit-test",
            "experiment": "exp-test",
            "target_temperature": 37.0,
            "stabilization_started_at_ts": 1000.0,
        },
        session=SimpleNamespace(status="in_progress", error=None, step_id="stabilize"),
    )


def poll_temperature(
    monkeypatch: pytest.MonkeyPatch, ctx: Any, timestamp: float, temperature: float, age: float = 0.0
) -> bool:
    from datetime import datetime, timezone

    monkeypatch.setattr(fta, "_now_ts", lambda: timestamp + age)
    monkeypatch.setattr(
        fta,
        "_fetch_current_temperature",
        lambda *_args: SimpleNamespace(
            temperature=temperature, timestamp=datetime.fromtimestamp(timestamp, timezone.utc)
        ),
    )
    return fta.BiasTrimStabilizeStep()._check_once(ctx)


def test_stability_requires_full_window_of_fresh_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = trim_context()
    for index in range(24):
        assert not poll_temperature(monkeypatch, ctx, 1000 + index * 5, 37 + (index % 2) * 0.01)
    assert poll_temperature(monkeypatch, ctx, 1120, 37.0)
    assert ctx.data["stable_temperature_estimate"] == pytest.approx(37.0)
    assert ctx.session.status == "in_progress"  # control stays on for the probe


@pytest.mark.parametrize(
    "temperatures",
    [
        [36.96 + i * 0.003 for i in range(25)],  # drift is allowed within the target band
        [36.94, 37.06] * 12 + [36.94],  # variation is allowed within the target band
    ],
)
def test_hold_accepts_movement_within_target_band(
    monkeypatch: pytest.MonkeyPatch, temperatures: list[float]
) -> None:
    ctx = trim_context()
    for index, temperature in enumerate(temperatures[:-1]):
        assert not poll_temperature(monkeypatch, ctx, 1000 + index * 5, temperature)
    assert poll_temperature(monkeypatch, ctx, 1120, temperatures[-1])


def test_retained_and_missing_readings_cannot_complete_window(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = trim_context()
    assert not poll_temperature(monkeypatch, ctx, 1000, 37.0)
    assert not poll_temperature(monkeypatch, ctx, 1000, 37.0, age=5)
    assert len(ctx.data["stability_history"]) == 1
    assert not poll_temperature(monkeypatch, ctx, 1000, 37.0, age=20)
    assert ctx.data["stability_history"] == []
    assert not poll_temperature(monkeypatch, ctx, 1025, 37.0)
    assert not poll_temperature(monkeypatch, ctx, 1050, 37.0)
    assert len(ctx.data["stability_history"]) == 1


@pytest.mark.parametrize("temperature,age", [(float("nan"), 0), (37.0, -1)])
def test_invalid_readings_reset_stability(
    monkeypatch: pytest.MonkeyPatch, temperature: float, age: float
) -> None:
    ctx = trim_context()
    assert not poll_temperature(monkeypatch, ctx, 1000, temperature, age)
    assert not ctx.data["is_stable"]


def test_timeout_applies_even_without_sensor_readings(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = trim_context()
    monkeypatch.setattr(fta, "_now_ts", lambda: 1000 + fta.STABILIZATION_TIMEOUT_S)
    stopped: list[dict] = []
    monkeypatch.setattr(fta, "_stop_bias_trim_jobs", lambda data: stopped.append(data))
    assert fta.BiasTrimStabilizeStep().advance(ctx) is None
    assert ctx.session.status == "failed"
    assert ctx.session.step_id == "ended"
    assert len(stopped) == 1


def test_mark_failed_and_stop_sets_session_state(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped_data: list[dict[str, Any]] = []
    monkeypatch.setattr(fta, "_stop_bias_trim_jobs", lambda data: stopped_data.append(dict(data)))

    session = SimpleNamespace(status="in_progress", error=None, step_id="stabilize")
    ctx: Any = SimpleNamespace(data={"job_source": "abc-123"}, session=session)

    fta._mark_failed_and_stop(ctx, "failed to stabilize")

    assert stopped_data == [{"job_source": "abc-123"}]
    assert session.status == "failed"
    assert session.error == "failed to stabilize"
    assert session.step_id == "ended"


def test_on_session_abort_uses_shared_stop_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped_data: list[dict[str, Any]] = []
    monkeypatch.setattr(fta, "_stop_bias_trim_jobs", lambda data: stopped_data.append(dict(data)))

    with_job_source: Any = SimpleNamespace(data={"job_source": "bias-trim-123"})
    fta.FIRTemperatureBiasTrimProtocol.on_session_abort(with_job_source)

    without_job_source: Any = SimpleNamespace(data={})
    fta.FIRTemperatureBiasTrimProtocol.on_session_abort(without_job_source)

    assert stopped_data == [
        {"job_source": "bias-trim-123"},
        {},
    ]


def test_bias_trim_target_step_starts_stirring_before_heating(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_start_stirring(_session_id: str) -> str:
        calls.append("stirring")
        return "ABC"

    def fake_start_heating(_session_id: str, _target: float) -> str:
        calls.append("heating")
        return "ABC"

    monkeypatch.setattr(fta, "_start_bias_trim_stirring", fake_start_stirring)
    monkeypatch.setattr(fta, "_start_bias_trim_heating", fake_start_heating)

    ctx: Any = SimpleNamespace(
        data={},
        inputs=SimpleNamespace(float=lambda _key: 37.0),
        session=SimpleNamespace(session_id="session-123"),
    )

    next_step = fta.BiasTrimTargetStep().advance(ctx)

    assert calls == ["stirring", "heating"]
    assert ctx.data["job_source"] == "ABC"
    assert isinstance(next_step, fta.BiasTrimStabilizeStep)


def test_start_temperature_automation_drops_skip_first_run(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAutomation:
        def __init__(
            self,
            unit: str,
            experiment: str,
            automation_name: str,
            **kwargs,
        ) -> None:
            self.unit = unit
            self.experiment = experiment
            self.automation_name = automation_name
            self.kwargs = kwargs

    monkeypatch.setitem(fta.available_temperature_automations, "_test_fake", FakeAutomation)

    job = cast(
        Any,
        fta.start_temperature_automation(
            automation_name="_test_fake",
            unit="unit-test",
            experiment="exp-test",
            skip_first_run=True,
            target_temperature=37.0,
        ),
    )

    assert job.unit == "unit-test"
    assert job.experiment == "exp-test"
    assert job.automation_name == "_test_fake"
    assert "skip_first_run" not in job.kwargs
    assert job.kwargs["target_temperature"] == 37.0


def test_update_heater_respects_pwm_lock() -> None:
    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    calls: list[float] = []

    def fake_update_heater(new_dc: float) -> bool:
        calls.append(new_dc)
        return True

    job._update_heater = fake_update_heater

    job.pwm = SimpleNamespace(is_locked=lambda: True)
    assert job.update_heater(50.0) is False
    assert calls == []

    job.pwm = SimpleNamespace(is_locked=lambda: False)
    assert job.update_heater(50.0) is True
    assert calls == [50.0]


def test_check_if_exceeds_max_temp_reduces_heater_gently_in_danger_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fta,
        "get_pioreactor_model",
        lambda: SimpleNamespace(
            max_temp_to_reduce_heating=50.0,
            max_temp_to_disable_heating=60.0,
            max_temp_to_shutdown=70.0,
        ),
    )

    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    job.heater_duty_cycle = 80.0
    job._requested_heater_duty_cycle = 80.0
    job._heater_state_lock = RLock()
    job.logger = SimpleNamespace(debug=lambda *_args, **_kwargs: None)
    reductions: list[float] = []

    def fake_update_heater(new_dc: float) -> bool:
        reductions.append(new_dc)
        return True

    job._update_heater = fake_update_heater

    observed_temp = job._check_if_exceeds_max_temp(55.0)

    assert observed_temp == pytest.approx(55.0)
    assert reductions == [pytest.approx(80.0 * fta.DANGER_ZONE_DUTY_CYCLE_REDUCTION_FACTOR)]


@pytest.mark.parametrize("stable_estimate", [37.0, 36.89])
def test_bias_trim_probe_step_ui_uses_store_estimator(
    monkeypatch: pytest.MonkeyPatch, stable_estimate: float
) -> None:
    base = SimpleNamespace(
        estimator_name="baseline-estimator",
        user_bias_c=0.2,
        w_obj=0.6,
        w_amb=0.2,
        w_pcb=0.1,
        c=1.5,
        tau_s=5.0,
        k_vol=0.05,
        volume_ref_ml=25.0,
        y="temperature_c",
    )
    monkeypatch.setattr(fta, "_load_active_fir_estimator", lambda: base)
    monkeypatch.setattr(fta, "_default_bias_trim_name", lambda _name: "trimmed-estimator")
    monkeypatch.setattr(fta, "get_unit_name", lambda: "unit-test")
    from datetime import datetime, timezone

    monkeypatch.setattr(fta, "_now_ts", lambda: 1100.0)
    monkeypatch.setattr(
        fta,
        "_fetch_current_temperature",
        lambda *_args: SimpleNamespace(
            temperature=37.02, timestamp=datetime.fromtimestamp(1100, timezone.utc)
        ),
    )
    monkeypatch.setattr(fta, "current_utc_datetime", lambda: "2020-01-01T00:00:00Z")

    class FakeAdjustedEstimator:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

        def save_to_disk_for_device(self, _device: str) -> str:
            raise AssertionError("UI flow should not call direct estimator save.")

        def set_as_active_calibration_for_device(self, _device: str) -> None:
            raise AssertionError("UI flow should not directly set active estimator.")

    monkeypatch.setattr(fta, "FIRTemperatureEstimator", FakeAdjustedEstimator)

    stopped_data: list[dict[str, Any]] = []
    monkeypatch.setattr(fta, "_stop_bias_trim_jobs", lambda data: stopped_data.append(dict(data)))

    store_calls: list[tuple[Any, str]] = []
    completed: dict[str, Any] = {}

    def fake_store_estimator(estimator: Any, device: str) -> dict[str, str]:
        store_calls.append((estimator, device))
        return {
            "device": device,
            "estimator_name": estimator.estimator_name,
            "path": "/tmp/ui-estimator.yaml",
        }

    ctx: Any = SimpleNamespace(
        mode="ui",
        data={
            "stable_temperature_estimate": stable_estimate,
            "job_source": "bias-trim-123",
            "unit": "unit-test",
            "experiment": "exp-test",
            "target_temperature": 37.0,
        },
        inputs=SimpleNamespace(float=lambda _key: 37.4),
        store_estimator=fake_store_estimator,
        complete=lambda payload: completed.update(payload),
        session=SimpleNamespace(status="in_progress", error=None, step_id="probe"),
    )

    result = fta.BiasTrimProbeStep().advance(ctx)

    assert result is None
    assert len(store_calls) == 1
    assert store_calls[0][1] == fta.TEMPERATURE_FIR_DEVICE
    assert completed["saved_path"] == "/tmp/ui-estimator.yaml"
    assert completed["estimator_name"] == "trimmed-estimator"
    assert completed["base_estimator_name"] == "baseline-estimator"
    assert completed["user_bias_c"] == pytest.approx(0.58)
    assert stopped_data == [ctx.data]


def test_bias_trim_probe_step_uses_store_estimator_even_if_mode_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = SimpleNamespace(
        estimator_name="baseline-estimator",
        user_bias_c=0.2,
        w_obj=0.6,
        w_amb=0.2,
        w_pcb=0.1,
        c=1.5,
        tau_s=5.0,
        k_vol=0.05,
        volume_ref_ml=25.0,
        y="temperature_c",
    )
    monkeypatch.setattr(fta, "_load_active_fir_estimator", lambda: base)
    monkeypatch.setattr(fta, "_default_bias_trim_name", lambda _name: "trimmed-estimator")
    monkeypatch.setattr(fta, "get_unit_name", lambda: "unit-test")
    from datetime import datetime, timezone

    monkeypatch.setattr(fta, "_now_ts", lambda: 1100.0)
    monkeypatch.setattr(
        fta,
        "_fetch_current_temperature",
        lambda *_args: SimpleNamespace(
            temperature=37.02, timestamp=datetime.fromtimestamp(1100, timezone.utc)
        ),
    )
    monkeypatch.setattr(fta, "current_utc_datetime", lambda: "2020-01-01T00:00:00Z")

    class FakeAdjustedEstimator:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    monkeypatch.setattr(fta, "FIRTemperatureEstimator", FakeAdjustedEstimator)

    stopped_data: list[dict[str, Any]] = []
    monkeypatch.setattr(fta, "_stop_bias_trim_jobs", lambda data: stopped_data.append(dict(data)))

    store_calls: list[tuple[Any, str]] = []
    completed: dict[str, Any] = {}

    def fake_store_estimator(estimator: Any, device: str) -> dict[str, str]:
        store_calls.append((estimator, device))
        return {
            "device": device,
            "estimator_name": estimator.estimator_name,
            "path": "/tmp/cli-estimator.yaml",
        }

    ctx: Any = SimpleNamespace(
        mode="cli",
        data={
            "stable_temperature_estimate": 37.0,
            "job_source": "bias-trim-123",
            "unit": "unit-test",
            "experiment": "exp-test",
            "target_temperature": 37.0,
        },
        inputs=SimpleNamespace(float=lambda _key: 37.4),
        store_estimator=fake_store_estimator,
        complete=lambda payload: completed.update(payload),
        session=SimpleNamespace(status="in_progress", error=None, step_id="probe"),
    )

    result = fta.BiasTrimProbeStep().advance(ctx)

    assert result is None
    assert len(store_calls) == 1
    assert store_calls[0][1] == fta.TEMPERATURE_FIR_DEVICE
    assert completed["saved_path"] == "/tmp/cli-estimator.yaml"
    assert completed["estimator_name"] == "trimmed-estimator"
    assert completed["base_estimator_name"] == "baseline-estimator"
    assert completed["user_bias_c"] == pytest.approx(0.58)
    assert stopped_data == [ctx.data]


def test_probe_outside_target_range_stays_on_form(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = trim_context()
    ctx.session.step_id = "probe"
    ctx.data["stable_temperature_estimate"] = 37.0
    ctx.inputs = SimpleNamespace(float=lambda _key: 37.4)
    poll_temperature(monkeypatch, ctx, 1000, 37.3)
    with pytest.raises(ValueError, match="outside the target range"):
        fta.BiasTrimProbeStep().advance(ctx)
    assert ctx.session.step_id == "probe"
    assert ctx.inputs.float("measured_temperature") == 37.4


def test_real_session_waits_then_prompts_without_stopping_control(monkeypatch: pytest.MonkeyPatch) -> None:
    from pioreactor.calibrations.session_flow import advance_session, get_session_step

    monkeypatch.setattr(fta, "_start_bias_trim_stirring", lambda _id: "source")
    monkeypatch.setattr(fta, "_start_bias_trim_heating", lambda _id, _target: "source")
    monkeypatch.setattr(fta, "_stop_bias_trim_jobs", lambda _data: pytest.fail("Stopped before measurement"))
    monkeypatch.setattr(fta, "_now_ts", lambda: 1000.0)
    session = fta.start_fir_temperature_bias_trim_session(fta.TEMPERATURE_FIR_DEVICE)
    registry = fta.FIRTemperatureBiasTrimProtocol.step_registry
    advance_session(registry, session, {})
    advance_session(registry, session, {"target_temperature": 37.0})
    assert get_session_step(registry, session).step_type == "wait"
    for index in range(25):
        from datetime import datetime, timezone

        timestamp = 1000 + index * 5
        monkeypatch.setattr(fta, "_now_ts", lambda: timestamp)
        monkeypatch.setattr(
            fta,
            "_fetch_current_temperature",
            lambda *_args: SimpleNamespace(
                temperature=37.0, timestamp=datetime.fromtimestamp(timestamp, timezone.utc)
            ),
        )
        advance_session(registry, session, {})
    assert session.step_id == "probe"
    assert get_session_step(registry, session).step_type == "form"


def test_probe_rejects_stale_reading_without_saving(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = trim_context()
    ctx.inputs = SimpleNamespace(float=lambda _key: 37.4)
    poll_temperature(monkeypatch, ctx, 1000, 37.0, age=30)
    with pytest.raises(ValueError, match="No fresh temperature"):
        fta.BiasTrimProbeStep().advance(ctx)


def test_stop_request_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fta, "_unit_api_address", lambda: "localhost")
    monkeypatch.setattr(fta, "post_into", lambda *args, **kwargs: SimpleNamespace(ok=False, status_code=500))
    with pytest.raises(ValueError, match="Unable to stop trim jobs"):
        fta._stop_bias_trim_jobs({"job_source": "trim-session"})


def test_timeout_remains_terminal_if_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = trim_context()

    def fail_cleanup(data: Any) -> None:
        raise ValueError("Worker unavailable")

    monkeypatch.setattr(fta, "_stop_bias_trim_jobs", fail_cleanup)
    fta._mark_failed_and_stop(ctx, "Timed out")
    assert ctx.session.status == "failed"
    assert ctx.session.error == "Timed out Cleanup failed: Worker unavailable"


@pytest.mark.parametrize("outside_temperature", [36.84, 37.16])
def test_leaving_target_band_restarts_entire_hold(
    monkeypatch: pytest.MonkeyPatch, outside_temperature: float
) -> None:
    ctx = trim_context()
    for index in range(24):
        assert not poll_temperature(monkeypatch, ctx, 1000 + index * 5, 37.0)
    assert not poll_temperature(monkeypatch, ctx, 1120, outside_temperature)
    assert ctx.data["stability_history"] == []
    for index in range(24):
        assert not poll_temperature(monkeypatch, ctx, 1125 + index * 5, 37.0)
    assert poll_temperature(monkeypatch, ctx, 1245, 37.0)


@pytest.mark.parametrize("temperature", [36.85, 37.15])
def test_hold_includes_target_band_boundaries(monkeypatch: pytest.MonkeyPatch, temperature: float) -> None:
    ctx = trim_context()
    for index in range(24):
        assert not poll_temperature(monkeypatch, ctx, 1000 + index * 5, temperature)
    assert poll_temperature(monkeypatch, ctx, 1120, temperature)
