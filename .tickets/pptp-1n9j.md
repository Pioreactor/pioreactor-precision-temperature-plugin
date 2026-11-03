---
id: pptp-1n9j
status: closed
deps: []
links: []
created: 2026-09-16T19:40:14Z
type: task
priority: 2
assignee: CamDavidsonPilon
---
# similar to /Users/camerondavidson-pilon/code/pioreactor-oled-status-display-plugin/oled_status_display/__init__.py, add a self-test that checks if i2c is populated


## Notes

**2026-11-03T19:25:59Z**

Already implemented in pioreactor_precision_temperature_plugin/__init__.py: registered MLX90632 I2C presence self-test using temperature_automation.mlx_address (default 0x3A), with an address-specific failure message. Added five passing pytest cases covering registration, default/configured addresses, and present/missing devices. Hardware probes are mocked; no physical-device validation performed.
