# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import types
from pathlib import Path


def _write_test_config(dot_pioreactor: Path) -> None:
    logs_dir = dot_pioreactor / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    (dot_pioreactor / "config.ini").write_text(
        textwrap.dedent(
            f"""
            [cluster.topology]
            leader_hostname=localhost
            leader_address=localhost

            [mqtt]
            broker_address=localhost
            broker_port=1883
            use_tls=0
            username=pioreactor
            password=raspberry

            [logging]
            log_file={logs_dir / "pioreactor.log"}
            ui_log_file={logs_dir / "ui.log"}
            console_log_level=DEBUG

            [stirring.config]
            initial_target_rpm=400
            """
        ).strip()
    )
    (dot_pioreactor / "unit_config.ini").write_text("")


def _install_runtime_stubs() -> None:
    if "busio" not in sys.modules:
        busio = types.ModuleType("busio")

        class I2C:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

        busio.I2C = I2C  # type: ignore[attr-defined]
        sys.modules["busio"] = busio

    if "rpi_hardware_pwm" not in sys.modules:
        rpi_hardware_pwm = types.ModuleType("rpi_hardware_pwm")

        class HardwarePWM:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

        rpi_hardware_pwm.HardwarePWM = HardwarePWM  # type: ignore[attr-defined]
        sys.modules["rpi_hardware_pwm"] = rpi_hardware_pwm

    # The installed pioreactor build imports calibration protocols eagerly, which
    # pulls in modules unrelated to this plugin's unit tests. Stub only the small
    # calibration API surface used by this plugin.
    calibrations_package = types.ModuleType("pioreactor.calibrations")
    calibrations_package.__path__ = []
    sys.modules["pioreactor.calibrations"] = calibrations_package

    registry = types.ModuleType("pioreactor.calibrations.registry")

    class CalibrationProtocol:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    registry.CalibrationProtocol = CalibrationProtocol  # type: ignore[attr-defined]
    sys.modules["pioreactor.calibrations.registry"] = registry

    session_flow = types.ModuleType("pioreactor.calibrations.session_flow")

    class SessionContext:
        def __init__(self) -> None:
            self.data: dict[str, object] = {}
            self.session = None
            self.mode = "cli"
            self.inputs = None

    class SessionStep:
        step_id = ""

    class _Fields:
        @staticmethod
        def float(*_args, **_kwargs):
            return {"type": "float"}

    class _Steps:
        @staticmethod
        def info(*_args, **_kwargs):
            return {"kind": "info"}

        @staticmethod
        def form(*_args, **_kwargs):
            return {"kind": "form"}

        @staticmethod
        def action(*_args, **_kwargs):
            return {"kind": "action"}

    session_flow.fields = _Fields  # type: ignore[attr-defined]
    session_flow.steps = _Steps  # type: ignore[attr-defined]
    session_flow.SessionContext = SessionContext  # type: ignore[attr-defined]
    session_flow.SessionStep = SessionStep  # type: ignore[attr-defined]
    session_flow.StepRegistry = dict  # type: ignore[attr-defined]
    sys.modules["pioreactor.calibrations.session_flow"] = session_flow

    structured_session = types.ModuleType("pioreactor.calibrations.structured_session")

    class CalibrationSession:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

        def complete(self, data) -> None:
            self.data = data
            self.status = "completed"

    def utc_iso_timestamp() -> str:
        return "2020-01-01T00:00:00Z"

    structured_session.CalibrationSession = CalibrationSession  # type: ignore[attr-defined]
    structured_session.utc_iso_timestamp = utc_iso_timestamp  # type: ignore[attr-defined]
    sys.modules["pioreactor.calibrations.structured_session"] = structured_session


_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

_dot_pioreactor = Path(tempfile.mkdtemp(prefix="pr-temp-plugin-tests-"))
_write_test_config(_dot_pioreactor)
os.environ["DOT_PIOREACTOR"] = str(_dot_pioreactor)
os.environ["GLOBAL_CONFIG"] = str(_dot_pioreactor / "config.ini")
os.environ["LOCAL_CONFIG"] = str(_dot_pioreactor / "unit_config.ini")
os.environ["TESTING"] = "1"
_install_runtime_stubs()
