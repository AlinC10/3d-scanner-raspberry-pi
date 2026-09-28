#### Motor Test

import time

from hardware.motor import Motor
from gpiozero import DigitalOutputDevice, DigitalInputDevice, InputDevice, OutputDevice
from hardware.relay import Relay

try:
    motor_x = Motor(dir_pin=18, step_pin=15, en_pin=14) # tija
    motor_y = Motor(dir_pin=7, step_pin=8, en_pin=25) # turntable
    
    # sleep_x = 14
    # reset_x = 15
    
    # DigitalOutputDevice(sleep_x).on()
    # DigitalOutputDevice(reset_x).on()
    # sleep = OutputDevice(14)
    # reset = OutputDevice(15)
    # sleep.on()
    # reset.on()
    # reset
    
    # DigitalOutputDevice(11).on()
    lights = Relay(pin=11, active_high=True, initial_value=False)
    lights.on()

    # endstop = InputDevice(pin=2, pull_up=False)
    
    print("Pornire motor...")
    motor_y.enable()
    while True:
        # Un Nema17 standard are 200 de pași pe rotație completă (fără microstepping)
        print("Se rotește 200 de pași în sens orar...")
        motor_y.rotate(steps=1600, clockwise=False, delay=0.001, verbose=True)
        
        time.sleep(2)

        # # print(endstop.value)
        # print("Se rotește 400 de pași în sens anti-orar, mai rapid...")
        # motor_y.rotate(steps=400, clockwise=False, delay=0.001)

        time.sleep(1)
except KeyboardInterrupt:
    motor_y.disable()
    motor_y.stop()
# lights = Relay(pin=11, active_high=True, initial_value=False)
# lights.on()
# while True:
#     pass


### 100% working Motor test
# from gpiozero import DigitalOutputDevice
# from time import sleep

# # Definim pinii de control
# # ATENȚIE: Înlocuiește aceste numere cu pinii GPIO reali pe care îi folosești pe Raspberry Pi!
# # Exemplu: dacă folosești pinii de data trecută, pune STEP_PIN = 21, DIR_PIN = 20.
# STEP_PIN = 5
# DIR_PIN = 16
# EN_PIN = 6

# # Setăm pinii ca ieșiri
# step_pin = DigitalOutputDevice(STEP_PIN)
# dir_pin = DigitalOutputDevice(DIR_PIN)
# en_pin = DigitalOutputDevice(EN_PIN)
# dir_pin.on()

# # Activăm motorul (TB6600 este activ pe starea LOW)
# en_pin.off()

# print("Sistem pornit. Incepem testul in 2 secunde...")
# sleep(2)

# try:
#     # Echivalentul funcției loop() din Arduino
#     while True:
#         # --- ROTAȚIE ÎNTR-UN SENS ---
#         print("-> Motorul ar trebui sa se invarta la DREAPTA (sens orar)...")
#         dir_pin.on()

#         # Executăm 200 de pași
#         for _ in range(10):
#             step_pin.on()
#             sleep(0.001)  # 1000 microsecunde = 0.001 secunde
#             step_pin.off()
#             sleep(0.001)
#         # sleep(2)

#         # print("-> Pauza de 1 secunda. Motorul sta pe loc.")
#         # sleep(1)

#         # # --- ROTAȚIE ÎN SENS OPUS ---
#         # print("-> Motorul ar trebui sa se invarta la STANGA (sens anti-orar)...")
#         # dir_pin.off()

#         # # Executăm 200 de pași înapoi
#         # for _ in range(1000):
#         #     step_pin.on()
#         #     sleep(0.0001)
#         #     step_pin.off()
#         #     sleep(0.0001)

#         # print("-> Pauza. Ciclul s-a terminat.")
#         # print("-----------------------------------")
#         # sleep(2)

# except KeyboardInterrupt:
#     # Când apeși CTRL+C pentru a opri scriptul, tăiem curentul la motor pentru siguranță
#     print("\nTest oprit de utilizator. Dezactivare motor.")
#     en_pin.on()

# from gpiozero import InputDevice

# endstop = InputDevice(pin=3, pull_up=True)

# while True:
#     print(endstop)