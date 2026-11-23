# G3 Firmware Release Notes (4.x)

## 4.3.0 — 9 September 2026

Originally planned for 1 September 2026. **Staged OTA rollout:** a release is offered to 5 % of controllers, then 25 %
after 24 hours, then 100 % after 72 hours; the rollout pauses automatically if the alarm rate of updated controllers
rises by more than 20 %. Adds WPA3 Wi-Fi. **Changed default:** `irrigation.max_cycle_sec` lowered from **900 to 600
seconds** as a safety change after PM-003. **Deprecation:** Cloud API v1 works until **31 March 2027**, then returns
HTTP 410 Gone.

## 4.2.1 — 14 January 2026

Fixes **VEL-2291**, the cause of incident PM-003: on 4.2.0 the current of VX-8 outputs V9–V16 was read with the
onboard scaling factor, so normal current was reported about 1.5× too high, causing false E-271 alarms; a driver reset
after the alarm could leave outputs energised. Adds a **hardware irrigation watchdog** that enforces
`irrigation.max_cycle_sec`. Raises the default `rs485.timeout_ms` from **500 to 800 ms**.

## 4.2.0 — 18 November 2025

Adds CO2 enrichment control (`climate.co2_target_ppm`, default 800 ppm, interlocked with vent position), batch OTA for
sensor nodes and valve open-circuit warning E-272. Known issue added 3 December 2025: false E-271 on V9–V16 (VEL-2291),
fixed in 4.2.1.

## 4.1.0 — 3 June 2025

Adds multi-zone irrigation scheduling (up to 16 zones with VX-8), **QR code pairing** of controllers in the Vellora Grow
app, EC calibration reminders (E-620 after 90 days) and a temporary Installer role that expires after 14 days.

## 4.0.0 — 11 February 2025

First major 4.x release. Adds **VPD control mode** (`climate.mode = vpd`, target 0.9 kPa), Modbus RTU master on both
RS-485 ports and per-port communication codes E-217 and E-218, which replace the old E-210. QR pairing is not yet
supported; enter the serial number manually.
