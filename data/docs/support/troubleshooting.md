# Troubleshooting Guide

## Modbus devices not answering (E-217 / E-218)

Always ask for the serial number and firmware version first. Then check, in order:

1. **Polarity:** swapped A/B wires are the most common cause on new installations.
2. **Termination:** exactly two 120 Ω terminators, one at each end of the daisy-chained bus. The G3's built-in
   terminator is DIP switch **S1** for port 1 and **S2** for port 2.
3. **Baud rate and address:** every device on the bus must use the same baud rate and a unique Modbus address.
4. **Slow devices:** raise `rs485.timeout_ms`. The default is 800 ms since firmware 4.2.1 (it was 500 ms); controllers
   configured on older firmware keep their old value.

## Valves and wireless sensors

- **E-271 on V9–V16 with firmware 4.2.0:** known false alarm, update to 4.2.1. Otherwise measure the coil.
- **Irrigation stops early after updating to 4.3.0:** the default `irrigation.max_cycle_sec` dropped from 900 to 600
  seconds. Set the value explicitly if long cycles are needed.
- **Sensor keeps going offline (E-312):** check lithium batteries and distance (about 60 m inside a full crop). Steel
  walls, cold stores and water tanks block the radio; an **SK-R1 repeater** usually solves it.
- An offline controller (green blinking LED) keeps controlling the greenhouse; check Ethernet/Wi-Fi and outbound port 443.

## Safe Mode and resets

After three failed boots the G3 enters **Safe Mode** (red LED, all outputs off except frost heating). Reinstall firmware
from the local service console. A **configuration reset** (hold reset **10 seconds**) returns settings to defaults but
keeps firmware and sensor pairings. A **factory reset** (hold **30 seconds**) also removes pairings and the cloud
registration and is only done on L2 instruction.

## Priorities

| Priority | Example | First response, Premium Care | First response, Standard |
|----------|---------|------------------------------|--------------------------|
| P1 | Crop at risk (frost alarm, valves stuck open) | 30 minutes, 24/7 | 4 business hours |
| P2 | One zone not working | 2 business hours | 1 business day |
| P3 | Question or minor issue | 1 business day | 2 business days |

Crop-damage cases are always P1 and escalated to L3 engineering.
