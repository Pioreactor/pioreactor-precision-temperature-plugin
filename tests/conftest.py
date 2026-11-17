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

            [storage]
            persistent_cache={dot_pioreactor / "persistent.sqlite"}
            temporary_cache={dot_pioreactor / "temporary.sqlite"}

            [stirring.config]
            initial_target_rpm=400

            [od_config.photodiode_channel]
            1=REF
            2=90
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

    # Load the self-test registry before replacing the calibration API below.
    import pioreactor.actions.self_test  # noqa: F401


_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

_dot_pioreactor = Path(tempfile.mkdtemp(prefix="pr-temp-plugin-tests-"))
_write_test_config(_dot_pioreactor)
os.environ["DOT_PIOREACTOR"] = str(_dot_pioreactor)
os.environ["GLOBAL_CONFIG"] = str(_dot_pioreactor / "config.ini")
os.environ["LOCAL_CONFIG"] = str(_dot_pioreactor / "unit_config.ini")
os.environ["TESTING"] = "1"
os.environ["MODEL_NAME"] = "pioreactor_40ml_XR"
os.environ["MODEL_VERSION"] = "1.5"
_install_runtime_stubs()
