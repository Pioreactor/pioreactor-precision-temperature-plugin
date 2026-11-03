# -*- coding: utf-8 -*-
from __future__ import annotations

from threading import Event
from threading import Thread
from types import SimpleNamespace
from typing import Any
from typing import Callable
from typing import cast
from unittest.mock import Mock

import pytest
from pioreactor.automations import events
from pioreactor.background_jobs.base import BackgroundJob

from pioreactor_precision_temperature_plugin import fir_temperature_automation as fta


class FakePWM:
    def __init__(self) -> None:
        self.is_cleaned_up = False
        self.writes: list[float] = []

    def is_locked(self) -> bool:
        return False

    def change_duty_cycle(self, duty_cycle: float) -> None:
        assert not self.is_cleaned_up
        self.writes.append(duty_cycle)

    def clean_up(self) -> None:
        self.is_cleaned_up = True


class FakeTimer:
    def __init__(
        self,
        interval: float,
        function: Callable[[], Any],
        *,
        run_immediately: bool = False,
        **kwargs: Any,
    ) -> None:
        self.function = function
        self.run_immediately = run_immediately
        self.is_paused = False
        self.cancelled = False

    def start(self) -> FakeTimer:
        if self.run_immediately:
            self.function()
        return self

    def pause(self) -> None:
        self.is_paused = True

    def unpause(self) -> None:
        self.is_paused = False

    def cancel(self, timeout: float | None = None) -> None:
        self.cancelled = True

    def is_alive(self) -> bool:
        return False


@pytest.fixture
def isolated_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real automation execution and state transitions; replace hardware and MQTT I/O."""

    def initialize(self: Any, unit: str, experiment: str) -> None:
        self.published_settings = {}
        self.unit = unit
        self.experiment = experiment
        self.logger = Mock()
        self.pub_client = Mock()
        self.state = self.INIT
        self._shutdown_started = False
        self._is_cleaned_up = False
        self._blocking_event = Event()

    def post_initialize(self: Any) -> None:
        self.set_state(self.READY)

    monkeypatch.setattr(BackgroundJob, "__init__", initialize)
    monkeypatch.setattr(BackgroundJob, "__post__init__", post_initialize)
    monkeypatch.setattr(BackgroundJob, "_publish_setting", lambda *_args: None)
    monkeypatch.setattr(fta.AutomationJob, "_send_details_to_mqtt", lambda *_args: None)
    monkeypatch.setattr(fta, "RepeatedTimer", FakeTimer)
    monkeypatch.setattr(fta.hardware, "is_heating_pcb_present", lambda: True)
    monkeypatch.setattr(fta.TemperatureAutomationJobFIR, "setup_pwm", lambda _self: FakePWM())
    monkeypatch.setattr(fta.TemperatureAutomationJobFIR, "_setup_heating_pcb_tmp_sensor", lambda _self: None)
    monkeypatch.setattr(fta.TemperatureAutomationJobFIR, "read_heating_pcb_temperature", lambda _self: 25.0)
    monkeypatch.setattr(
        fta.TemperatureAutomationJobFIR,
        "_load_active_fir_estimator",
        lambda _self: SimpleNamespace(
            w_obj=1.0,
            w_amb=0.0,
            w_pcb=0.0,
            c=0.0,
            k_vol=0.0,
            volume_ref_ml=25.0,
            user_bias_c=0.0,
            tau_s=90.0,
        ),
    )


@pytest.fixture
def job(isolated_runtime: None) -> Any:
    return fta.OnlyRecordTemperature(unit="unit-test", experiment="lifecycle-test")


def publish_temperature(job: Any) -> None:
    job._set_latest_temperature(
        fta.structs.Temperature(temperature=30.0, timestamp=fta.current_utc_datetime())
    )


def test_temperature_execution_waits_until_subclass_init_finishes(
    isolated_runtime: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ported from core: a timer that runs immediately must see complete subclass state.
    observed: list[bool] = []

    class ImmediatelyRunningTimer(FakeTimer):
        def start(self) -> ImmediatelyRunningTimer:
            self.function()
            return self

    monkeypatch.setattr(fta, "RepeatedTimer", ImmediatelyRunningTimer)

    class StartupProbe(fta.TemperatureAutomationJobFIR):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.subclass_initialized = True

        def execute(self) -> None:
            observed.append(hasattr(self, "subclass_initialized"))

    probe = StartupProbe(unit="unit-test", experiment="startup-test")
    assert observed and all(observed)
    cast(Mock, probe.logger.error).assert_not_called()


def test_startup_infers_once(job: Any) -> None:
    assert job.latest_temperature == 25.0
    assert job.previous_temperature is None
    assert job._latest_run_at is not None


def test_inference_keeps_heater_running(job: Any) -> None:
    job.update_heater(40.0)
    writes_before = list(job.pwm.writes)
    job.infer_temperature()
    assert job.heater_duty_cycle == 40.0
    assert job.pwm.writes == writes_before


def test_heater_resumes_requested_power_after_pause(job: Any) -> None:
    # Ported from core's temperature automation tests.
    job.update_heater(30.0)
    job.set_state(job.SLEEPING)
    assert job.heater_duty_cycle == 0.0
    assert job.publish_temperature_timer.is_paused
    job.set_state(job.READY)
    assert job.heater_duty_cycle == 30.0
    assert not job.publish_temperature_timer.is_paused


def test_heater_update_while_paused_is_applied_on_resume(job: Any) -> None:
    # Ported from core, including a delta against requested rather than applied power.
    job.update_heater(30.0)
    job.set_state(job.SLEEPING)
    assert job.update_heater(20.0)
    assert job.update_heater_with_delta(5.0)
    assert job.heater_duty_cycle == 0.0
    job.set_state(job.READY)
    assert job.heater_duty_cycle == 25.0


def test_safety_reduction_while_paused_changes_requested_power(
    job: Any,
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
    job.update_heater(80.0)
    job.set_state(job.SLEEPING)
    job._check_if_exceeds_max_temp(55.0)
    assert job.heater_duty_cycle == 0.0
    job.set_state(job.READY)
    assert job.heater_duty_cycle == round(80.0 * fta.DANGER_ZONE_DUTY_CYCLE_REDUCTION_FACTOR, 2)


def test_safety_disable_while_paused_stays_off_on_resume(
    job: Any,
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
    job.blink_error_code = Mock()
    job.update_heater(80.0)
    job.set_state(job.SLEEPING)
    job._check_if_exceeds_max_temp(65.0)
    job.set_state(job.READY)
    assert job.heater_duty_cycle == 0.0


def test_temperature_execution_uses_shared_error_handling(job: Any) -> None:
    def fail() -> None:
        raise RuntimeError("control failed")

    job.execute = fail
    publish_temperature(job)
    assert isinstance(job.latest_event, events.ErrorOccurred)
    assert job.logger.error.call_args.args[0].args == ("control failed",)


def test_temperature_execution_does_not_overlap(job: Any) -> None:
    job.execute = Mock()
    with job._automation_execution_lock:
        publish_temperature(job)
    job.execute.assert_not_called()
    publish_temperature(job)
    job.execute.assert_called_once()


def test_temperature_execution_stops_during_shutdown(job: Any) -> None:
    job.execute = Mock()
    job._shutdown_started = True
    publish_temperature(job)
    job.execute.assert_not_called()


def test_inflight_execution_cannot_heat_while_sleeping(job: Any) -> None:
    entered, release = Event(), Event()

    def execute() -> None:
        entered.set()
        assert release.wait(2.0)
        job.update_heater(30.0)

    job.execute = execute
    runner = Thread(target=publish_temperature, args=(job,))
    runner.start()
    try:
        assert entered.wait(2.0)
        job.set_state(job.SLEEPING)
    finally:
        release.set()
        runner.join(2.0)
    assert not runner.is_alive()
    assert job.heater_duty_cycle == 0.0
    assert job.pwm.writes and all(dc == 0.0 for dc in job.pwm.writes)
    job.set_state(job.READY)
    assert job.heater_duty_cycle == 30.0


def test_disconnect_drains_execution_and_prevents_reheating(job: Any) -> None:
    entered, release, disconnected = Event(), Event(), Event()

    def execute() -> None:
        entered.set()
        assert release.wait(2.0)
        job.update_heater(80.0)

    def disconnect() -> None:
        job.set_state(job.DISCONNECTED)
        disconnected.set()

    job.execute = execute
    runner = Thread(target=publish_temperature, args=(job,))
    stopper = Thread(target=disconnect)
    runner.start()
    try:
        assert entered.wait(2.0)
        stopper.start()
        assert job._automation_stopping.wait(2.0)
        assert not disconnected.is_set()
    finally:
        release.set()
        runner.join(2.0)
        if stopper.ident is not None:
            stopper.join(2.0)
    assert not runner.is_alive() and not stopper.is_alive()
    assert disconnected.is_set()
    assert job.heater_duty_cycle == 0.0
    assert all(dc == 0.0 for dc in job.pwm.writes)
    assert job.pwm.is_cleaned_up
    assert job.publish_temperature_timer.cancelled
    assert job.read_external_temperature_timer.cancelled


def test_shared_periodic_timer_pauses_resumes_and_cancels(job: Any) -> None:
    timer = FakeTimer(1.0, lambda: None)
    job._automation_timer = timer
    job.set_state(job.SLEEPING)
    assert timer.is_paused
    job.set_state(job.READY)
    assert not timer.is_paused
    job.set_state(job.DISCONNECTED)
    assert timer.cancelled


@pytest.mark.parametrize("error_type", [ValueError, fta.exc.PWMError])
def test_update_heater_ignores_pwm_cleanup_race(job: Any, error_type: type[Exception]) -> None:
    # Ported from core: cleanup can occur after the initial property check.
    class PwmCleanedDuringChange(FakePWM):
        def change_duty_cycle(self, duty_cycle: float) -> None:
            self.is_cleaned_up = True
            raise error_type("PWM is closed")

    job.pwm = PwmCleanedDuringChange()
    assert job.update_heater(30.0) is False


@pytest.mark.parametrize("error_type", [ValueError, fta.exc.PWMError])
def test_update_heater_preserves_unrelated_pwm_errors(job: Any, error_type: type[Exception]) -> None:
    job.pwm.change_duty_cycle = Mock(side_effect=error_type("invalid PWM operation"))
    with pytest.raises(error_type, match="invalid PWM operation"):
        job.update_heater(30.0)
