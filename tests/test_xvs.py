import time
from concurrent.futures import ThreadPoolExecutor
from picamera2 import Picamera2
import logging

logging.basicConfig(level=logging.INFO)

def init_master():
    cam0 = Picamera2(0)
    config = cam0.create_preview_configuration(main={"size": (4056, 3040)})
    cam0.configure(config)
    cam0.start()
    return cam0

def init_slave():
    cam1 = Picamera2(1)
    config = cam1.create_preview_configuration(main={"size": (4056, 3040)})
    cam1.configure(config)
    cam1.start()
    return cam1

executor = ThreadPoolExecutor(max_workers=2)

f_master = executor.submit(init_master)
time.sleep(1)
f_slave = executor.submit(init_slave)

master = f_master.result()
slave = f_slave.result()

print("Both started. Wait a second for exposure to settle...")
time.sleep(2)

print("Capturing using request queues...")

def capture_slave():
    req = slave.capture_request()
    req.save("main", "test_photo_slave_1.jpg")
    req.release()
    print("Slave done!")

def capture_master():
    req = master.capture_request()
    req.save("main", "test_photo_master_0.jpg")
    req.release()
    print("Master done!")

f_cap_s = executor.submit(capture_slave)
f_cap_m = executor.submit(capture_master)

f_cap_s.result()
f_cap_m.result()

slave.stop()
master.stop()
print("Success!")
