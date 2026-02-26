# Pioreactor Precision Temperature Plugin

Installable Pioreactor plugin providing FIR-based liquid temperature estimation and thermostat automation using:

- MLX90632 object temperature
- MLX90632 ambient temperature
- heating PCB temperature

It also includes a one-point bias-trim calibration flow and seeds a default estimator YAML on install.

## Install from source

```bash
pio plugins install pioreactor_precision_temperature_plugin --source /path/to/pioreactor-precision-temperature-plugin
```

## Post-install behavior

The plugin `post_install.sh` script will:

1. Copy `fir_temperature_estimator_linear_mlx_ambient_pcb_v1.yaml` into:
   `$DOT_PIOREACTOR/storage/estimators/temperature_fir/`
2. Set it active using:
   `pio estimators set-active --device temperature_fir --name fir_temperature_estimator_linear_mlx_ambient_pcb_v1`

If `DOT_PIOREACTOR` is not set, it defaults to `$HOME/.pioreactor`.

## Configuration defaults

```ini
[temperature_automation.thermostat]
Kp=0.025
Ki=0.0
Kd=13.0
```

## Vendored dependency

This plugin vendors Adafruit's `adafruit_mlx90632.py` source from:
<https://github.com/adafruit/Adafruit_CircuitPython_MLX90632>

License text is included at:
`pioreactor_precision_temperature_plugin/THIRD_PARTY_LICENSE_ADAFRUIT_MLX90632.txt`
