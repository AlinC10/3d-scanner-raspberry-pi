# Information about the project

The brain of the hardware is a Raspberry Pi 5 (8GB) that concurrently orchestrates stepper motors, dual synchronized cameras, for a 3D scanner.
The objectives of this project are to make cameras work, and have the code well structure, to use them do different tasks:

* have the vcm driver auto-selected for every camera when is instantiated - Done
* have propreties for the photo resolution, quality for jpeg, video  resolution, preview resolution, rotation that can be set during the instantiation - Done
* preview - Done
* photo with a fixed focus - Done
* auto-exposure/ white balancing and things like this (and fixed) - Done
* have a method to rotate camera image (90 degrees left or right from every position) - the options should be left, right or something that would turn 180 (give it a representative name) - Done
* take picture in jpeg format and select the quality (default to 95) - Done

## Components

* **Single Board Computer:** Raspberry PI 5 8GB
* **OS:** Raspberry PI OS Trixie
* **Cameras:** 2x Arducam B0272 IMX477 Motorized - Mounted on a horizontal bracket at a 30° to 45° angle to each other.
Hardware synchronized via XVS (Master-Slave configuration) to ensure simultaneous capture from both angles. (not connected yet through XVS, the current implementetation will be for the Camera class that should control one camera and for DualCamera class that will control the cameras through multi-threading, after testing that both works like this i will connect them through XVS)

## Camera Information

**Link:** https://www.welectron.com/Arducam-B0272-12MP-IMX477-Motorized-Focus-High-Quality-Camera-for-Raspberry-Pi_1

**Arducam B0272 12MP IMX477 Motorized Focus High Quality Camera for Raspberry Pi**

### Description

This Arducam IMX477 High Quality motorized focus camera module integrates a motor that could be controlled by software for a smarter focus, and you will no longer focus the camera by screwing the lens with your bare hands. Instead, keyboard keys or OpenCV can be used to remotely control the focusing process of your HQ camera.

The same 12MP IMX477 High Quality Camera, but with smarter focus control – No more hand touching.

The intuitive focus control: Fine-tune the focus with keyboard arrow keys.
The automated focus control: Autofocus with OpenCV examples
Focus motor controlled via camera I2C.

**Features**
12MP IMX477 High Quality Camera - The same image sensor used in Raspberry Pi High Quality Camera. Natively works with existing commands, codes and examples.
I2C Focus Control - The focusing process is controlled via software instead of your bare hands. Use keyboard arrows keys to adjust the focus to the best or OpenCV autofocus examples to automate it.
Focus Distance: 80mm to infinity

### Specifications

#### Image Sensor

Sensor Model: IMX477
Shutter Type: Rolling shutter
Sensor resolution: 4056 x 3040 pixels, 12.3MP
Image Sensor Format: Type 1/2.3"
Pixel Size: 1.55 µm x 1.55 µm

#### Lens Assembly

Model No: M23390H08
Optical Format: 1/2.3"
Focal Length: 3.9mm
Aperture: F2.8
Field of View (FOV): 75° (H) on Raspberry Pi High Quality Camera, 50°(H) on Raspberry Pi V1/V2 Camera
Mount: M12 mount
Back Focal Length: 4.49mm
MOD: 0.3m
Dimension: F14×18.67mm
Weight: 5g

#### Camera Board

Board Size: 38mm×38mm
Hole Pitch: Compatible with 29mm, 30mm, 34mm
