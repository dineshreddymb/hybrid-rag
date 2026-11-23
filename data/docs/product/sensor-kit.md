# Sensor Kit — SK-200, SP-10, SK-R1

## SK-200 wireless sensor node

Battery-powered node hung at crop height. Measures air temperature (−20 to +60 °C, ±0.3 °C), humidity (±2 % RH),
CO2 (400–5000 ppm, NDIR) and PAR light (0–2500 µmol/m²/s).

- Batteries: 2 × AA **lithium** cells (alkaline cells fail in the cold). Battery life is about **18 months** at the
  default 60-second reporting interval and about **7 months** at 15 seconds.
- Radio range: 150 m line of sight, plan for about **60 m** inside a greenhouse with a full crop. Metal structures
  block the signal.
- Pairing: put the G3 in pairing mode in the app (LED blinks blue), then hold the node button for **5 seconds**.
- Alarms: **E-312** when a node is silent for more than 10 minutes, **E-305** when the battery is below 15 %.
- CO2 calibration: automatic baseline calibration assumes the lowest reading in a rolling **7-day** window is outdoor
  air (about 400 ppm). Greenhouses that dose CO2 for a week without venting should run a manual fresh-air calibration.

## SP-10 soil probe and SK-R1 repeater

The **SP-10** is a wired probe on an RS-485 port (Modbus address 10–29) measuring substrate moisture, EC (0–10 dS/m)
and temperature. It must be calibrated every **90 days**; overdue calibration raises **E-620**.

The **SK-R1** repeater extends the radio range where distance or metal (steel walls, cold stores, water tanks) blocks
the signal. It is mains powered (24 VAC or 12 V DC), relays up to **16 nodes**, and at most **2 repeaters** can be
chained between a node and the controller.
