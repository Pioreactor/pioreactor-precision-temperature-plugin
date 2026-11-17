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

## One-point temperature trim

Use the usual vial, liquid volume, stir bar, and external temperature probe.
Enter the target temperature and continue. Heating and stirring start, and the
waiting page advances automatically when the estimate is stable. Abort remains
available throughout the wait. Keep this page open while it checks progress.

The estimate must remain within ±0.15°C of the target for two continuous minutes,
checked approximately every five seconds.
Leaving the band resets the timer; slope and total variation within the band do
not affect acceptance.

Duplicate or stale readings cannot establish stability. Missing readings or gaps
longer than 15 seconds restart the window. The wait fails after 90 minutes and
requests shutdown of the jobs started by this session.

Once stable, heating and stirring continue while you enter the settled external
probe reading. Continue checks the current sensor reading, saves a new estimator,
and stops the session's jobs. Submission never returns to the waiting page.
If the current reading is stale or outside the target range, the form keeps your
input and explains why the trim cannot be saved. Repeated trims add
the measured correction to the existing estimator bias.

This flow requires the core and frontend changes that add `steps.wait()`; update
both alongside the plugin. Stability thresholds are initial engineering defaults,
informed by recent cluster temperature data, and still need physical validation.
