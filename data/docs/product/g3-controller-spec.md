# G3 Controller — Technical Specification

## Overview

Model **VG3-100** (EU, 868 MHz sensor radio) and **VG3-100-US** (915 MHz). The G3 runs all climate and irrigation logic
locally and keeps controlling the greenhouse when the internet or Vellora Cloud is unavailable. It stores **30 days**
of data at 1-minute resolution and uploads it when reconnected. Warranty: **3 years** from installation (or 3.5 years
from shipment, whichever ends first).

## Specifications

| Parameter | Value |
|-----------|-------|
| Supply | 24 VAC ±15 %, 40 VA max (use a transformer of at least 50 VA) |
| Relay outputs | 12 × 10 A @ 250 VAC |
| Valve outputs | 8 × 24 VAC, 1.0 A each; overcurrent trip at 1.2 A for > 200 ms |
| Valve expansion | 1 × VX-8 module adds V9–V16 (16 valves max) |
| Analog inputs | 8 × 0–10 V or 4–20 mA |
| RS-485 | 2 ports, Modbus RTU, 9600–115200 baud |
| Network | Ethernet 10/100, Wi-Fi 2.4 GHz (WPA3 from firmware 4.3.0) |
| Sensor nodes | up to **32 SK-200** per controller |
| Zones | up to 4 climate zones and 16 irrigation zones |
| Operating temperature | **−10 °C to +50 °C** |
| Storage temperature | −30 °C to +70 °C |
| Protection / weight | IP65, 1.8 kg, 220 × 160 × 65 mm |

## Status LED

Green steady: normal, cloud connected. Green blinking: normal, no cloud connection (local control continues).
Amber: active warning. Red: alarm or Safe Mode. Blue blinking: pairing mode for sensor nodes.
