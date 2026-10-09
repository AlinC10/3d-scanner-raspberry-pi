class ScannerState:
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    READY = "READY"
    SCANNING = "SCANNING"
    ERROR = "ERROR"

class ScannerMock:
    def __init__(self, xvs=False):
        self.state = ScannerState.IDLE
        self.turntable_motor = None
        self.z_axis_motor = None
        self.lights = None
        self.tof_sensor = None
        self.dual_cameras = None

    def finish_preparation(self, **kwargs):
        self.state = ScannerState.READY

    def start_scan(self, config, runpod_api_key, turnstile_token):
        self.state = ScannerState.SCANNING
        
    def abort_scan(self):
        self.state = ScannerState.IDLE

    def cleanup(self):
        pass

    def emergency_stop(self):
        pass

    def close_hardware(self):
        pass
        
    def get_progress(self):
        return {"state": self.state, "progress": 0}
