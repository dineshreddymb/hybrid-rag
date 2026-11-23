# G3 Error Code Reference

## Codes

Warnings (amber LED) let control continue; alarms (red LED) stop part of the control or put the crop at risk.

| Code | Severity | Meaning | What to do |
|------|----------|---------|------------|
| E-117 | Warning | 24 VAC supply below 19 VAC for > 2 s (brownout) | Check transformer is at least 50 VA |
| E-127 | Alarm | Relay contact stuck | Replace relay board VG3-RB12 |
| E-217 | Warning | **RS-485 port 1** timeout: Modbus device did not answer | Check A/B polarity, termination (DIP **S1**), baud, address |
| E-218 | Warning | **RS-485 port 2** timeout | As E-217, termination is DIP **S2** |
| E-271 | Alarm | **Irrigation valve overcurrent**: > 1.2 A for > 200 ms; cycle aborted | Check solenoid coil (20–40 Ω) and cable |
| E-272 | Warning | Valve open circuit (< 0.05 A when on) | Check cable and coil |
| E-305 | Warning | Sensor node battery below 15 % | Replace with lithium AA cells |
| E-312 | Warning | Sensor node offline > 10 min | Check batteries, distance, metal; consider SK-R1 |
| E-401 | Warning | Cloud TLS certificate error | Check clock; many sites at once = Vellora Cloud problem |
| E-402 | Warning | No NTP time sync for 24 h | Allow UDP port 123 |
| E-500 | Alarm | Configuration checksum mismatch | Restore configuration from Vellora Cloud |
| E-612 | Warning | CO2 reading below 300 or above 5000 ppm; dosing paused | Manual fresh-air calibration |
| E-620 | Warning | SP-10 EC probe calibration overdue (> 90 days) | Calibrate with reference solution |

## Known issue: false E-271 on firmware 4.2.0

On firmware **4.2.0**, E-271 could be raised falsely on outputs **V9–V16** (VX-8 expansion module). Do not replace
valves because of E-271 on those outputs; update to firmware **4.2.1**, which fixes engineering issue **VEL-2291**.
