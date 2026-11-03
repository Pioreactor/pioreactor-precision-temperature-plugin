# -*- coding: utf-8 -*-
from configparser import ConfigParser
from typing import Any
from typing import cast
from unittest.mock import Mock

import pytest
from pioreactor.actions.self_test import REGISTERED_SELF_TESTS

import pioreactor_precision_temperature_plugin as plugin


def test_mlx90632_self_test_is_registered() -> None:
    assert plugin.test_mlx90632_is_present in REGISTERED_SELF_TESTS


@pytest.mark.parametrize("configured_address,expected_address", [(None, 0x3A), ("0x3B", 0x3B)])
@pytest.mark.parametrize("present", [True, False])
def test_mlx90632_self_test_checks_configured_address(
    monkeypatch: pytest.MonkeyPatch,
    configured_address: str | None,
    expected_address: int,
    present: bool,
) -> None:
    config = ConfigParser()
    if configured_address is not None:
        config.read_dict({"temperature_automation": {"mlx_address": configured_address}})
    monkeypatch.setattr(plugin, "config", config)
    probe = Mock(return_value=present)
    monkeypatch.setattr(plugin, "is_i2c_device_present", probe)

    args = (cast(Any, None), cast(Any, None), "unit-test", "self-test")
    if present:
        plugin.test_mlx90632_is_present(*args)
    else:
        with pytest.raises(
            AssertionError,
            match=f"MLX90632 is not responding at I2C address {expected_address:#04x}",
        ):
            plugin.test_mlx90632_is_present(*args)

    probe.assert_called_once_with(expected_address)
