# G3 Configuration Reference

## Settings

Defaults are for **firmware 4.3.0**. Settings can be changed in the app or with `PATCH /v2/controllers/{id}/config`.

| Key | Default | Range | Notes |
|-----|---------|-------|-------|
| `climate.mode` | `temp_humidity` | `temp`, `temp_humidity`, `vpd` | `vpd` needs firmware 4.0.0+ |
| `climate.vpd_target_kpa` | `0.9` | 0.3–2.0 | Target VPD in VPD mode |
| `climate.night_setback_c` | `4.0` | 0–12 | Degrees the setpoint drops at night |
| `climate.co2_target_ppm` | `800` | 400–1500 | CO2 enrichment target (4.2.0+) |
| `climate.co2_max_vent_pct` | `20` | 0–100 | CO2 dosing stops above this vent opening |
| `irrigation.max_cycle_sec` | `600` | 30–3600 | Longest valve-open time per cycle. **Was 900 before 4.3.0** |
| `irrigation.parallel_valves` | `2` | 1–4 | Valves open at the same time |
| `rs485.timeout_ms` | `800` | 100–5000 | Modbus reply timeout. **Was 500 before 4.2.1** |
| `rs485.retries` | `3` | 0–10 | Retries before E-217/E-218 |
| `safety.max_temp_alarm_c` | `38` | 25–50 | Critical over-temperature alarm |
| `safety.frost_alarm_c` | `3` | −5–10 | Frost alarm, forces frost heating |
| `sensors.report_interval_s` | `60` | 15–300 | Shorter interval reduces battery life |

## Notes on changed defaults

Controllers keep any value that was set explicitly when they are upgraded; only sites using the default get the new
value. Since firmware 4.2.1 a **hardware watchdog** also enforces `irrigation.max_cycle_sec`: if a valve stays on longer
than the maximum plus 30 seconds, all valve outputs are switched off in hardware, even if the main firmware hangs.
