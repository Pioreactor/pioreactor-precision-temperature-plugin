# -*- coding: utf-8 -*-
from pioreactor.actions.self_test import register_self_tests
from pioreactor.config import config
from pioreactor.hardware import is_i2c_device_present
from pioreactor.logging import CustomLogger
from pioreactor.utils import managed_lifecycle

from pioreactor_precision_temperature_plugin.fir_temperature_automation import click_temperature_automation
from pioreactor_precision_temperature_plugin.fir_temperature_automation import FIRTemperatureBiasTrimProtocol
from pioreactor_precision_temperature_plugin.fir_temperature_automation import OnlyRecordTemperature
from pioreactor_precision_temperature_plugin.fir_temperature_automation import Thermostat


def test_mlx90632_is_present(
    managed_state: managed_lifecycle, logger: CustomLogger, unit: str, experiment: str
) -> None:
    address = int(config.get("temperature_automation", "mlx_address", fallback="0x3A"), 16)
    assert is_i2c_device_present(address), f"MLX90632 is not responding at I2C address {address:#04x}."


register_self_tests(test_mlx90632_is_present)


__all__ = [
    "Thermostat",
    "OnlyRecordTemperature",
    "click_temperature_automation",
    "FIRTemperatureBiasTrimProtocol",
]
