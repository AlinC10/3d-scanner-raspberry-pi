# System Configuration TODO List

* [x] **Endpoint with the connection status of Wi-Fi, Bluetooth, Ethernet** (what is connected, not only the power): `GET /system/status` (2026-10-29), see `../docs/SYSTEM_API.md`. The front-end polls it every 5-15 s.
* [x] **Save network credentials and use them before asking for the password**: saved profiles (`GET /system/wifi/saved`, `saved` in the scan, connect without password), and the connections are made again at startup (2026-10-29).
