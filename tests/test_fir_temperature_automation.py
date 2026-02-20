# -*- coding: utf-8 -*-
from __future__ import annotations

import math
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
    job._volume_ml = 30.0

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


def test_update_volume_from_mqtt_ignores_invalid_payloads() -> None:
    job: Any = object.__new__(fta.TemperatureAutomationJobFIR)
    job._volume_ml = 25.0
    job.logger = SimpleNamespace(debug=lambda *_args, **_kwargs: None)

    job._update_volume_from_mqtt(SimpleNamespace(payload="31.5"))
    assert job._volume_ml == pytest.approx(31.5)

    job._update_volume_from_mqtt(SimpleNamespace(payload="not-a-number"))
    assert job._volume_ml == pytest.approx(31.5)

    job._update_volume_from_mqtt(SimpleNamespace(payload=None))
    assert job._volume_ml == pytest.approx(31.5)


def test_windowed_history_and_slope() -> None:
    history = [
        {"ts": 100.0, "temperature": 30.0},
        {"ts": 350.0, "temperature": 33.0},
        {"ts": 600.0, "temperature": 38.0},
    ]
    window = fta._windowed_history(history)

    assert window == [
        {"ts": 350.0, "temperature": 33.0},
        {"ts": 600.0, "temperature": 38.0},
    ]

    slope = fta._compute_window_slope_c_per_min(window)
    expected = (38.0 - 33.0) / ((600.0 - 350.0) / 60.0)
    assert slope == pytest.approx(expected)
    assert math.isinf(fta._compute_window_slope_c_per_min([]))


def test_update_stabilization_state_marks_context_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx: Any = SimpleNamespace(
        data={
            "stability_history": [
                {"ts": 700.0, "temperature": 37.05, "error_abs": 0.05},
                {"ts": 995.0, "temperature": 37.00, "error_abs": 0.00},
            ]
        }
    )
    monkeypatch.setattr(fta, "_now_ts", lambda: 1000.0)

    fta._update_stabilization_state(ctx, measured_temperature=37.02, target_temperature=37.0)

    assert bool(ctx.data["is_stable"]) is True
    assert ctx.data["latest_estimated_temperature"] == pytest.approx(37.02)
    assert ctx.data["latest_abs_error"] == pytest.approx(0.02)
    assert abs(float(ctx.data["latest_slope_c_per_min"])) <= fta.STABILIZATION_MAX_SLOPE_C_PER_MIN


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
