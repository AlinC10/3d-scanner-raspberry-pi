# System Configuration TODO List

* [ ] **Create Endpoint to fetch WI-FI, Bluetooth, Ethernet connection status** with the last device known, not only the power status of this ports. 
* [ ] **Create an Endpoint to fetch WI-FI, Bluetooth, Ethernet status every 5-15 seconds in** `router/system/__init__.py`
```python
@router.get("/status")
def get_system_connectivity_status():
    """Returns aggregated status in a single HTTP request."""
    return {
        "wifi": wifi.get_status(),
        "bluetooth": bluetooth.get_power_status(),
        "ethernet": ethernet.get_status()
    }
```