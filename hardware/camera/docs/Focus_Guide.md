# Focus Calibration & Focus Stacking Guide

This document outlines how to calibrate your Arducam IMX477 Voice Coil Motors (VCM) for specific physical distances, why Depth of Field (DoF) is usually sufficient for turntable photogrammetry, and the pitfalls of using focus stacking (like `enfuse`) with Meshroom.

## 1. The 25cm Calibration Trick (Finding the Sweet Spot)

Because the VCM is an arbitrary magnetic elevator (0-1023) and the baseline depends entirely on how you twisted the manual outer lens barrel, there is no mathematical formula to convert a VCM number directly to centimeters.

To get your focus perfectly set at **25 cm** (the ideal sweet spot for a 26cm turntable placed 20cm away):

1. **Set up a physical target**: Tape a piece of paper with sharp, high-contrast text (like a printed QR code or barcode) to a box.
2. **Measure exactly 25 cm**: Use a tape measure and place the target exactly 25 cm away from the front glass of the camera lenses.
3. **Run your Autofocus Sweep**: Run a script that calls `dual_cam.focus_sweep_autofocus()` on your cameras (or open a live preview and manually adjust the VCM step-by-step until the text is perfectly sharp).
4. **Write down the magic number!**: When the sweep finishes, look at the VCM value it landed on (e.g., `680`).

Because you are never touching the outer manual barrels again, **that VCM number is permanently your 25cm sweet spot.** You can now hardcode `dual_cam.focus_set(680)` into your scanning script.

## 2. The "Turntable Secret" (Why you probably don't need stacking)

If your camera is 20 cm from the front edge of a 26 cm turntable, the back of the turntable is 46 cm away. You might think you need a massive Depth of Field to keep the whole 26 cm object in focus. You don't!

**The camera cannot see the back of the object.** 
Meshroom extracts all of its 3D feature points from the surfaces facing the camera. As long as you have enough Depth of Field to cover the front **half** of the object (the 13 cm radius), you are completely fine. When the turntable rotates 180 degrees, the blurry back of the object becomes the front, and it enters the razor-sharp zone.

By setting your focus at **25 cm** (using the trick above), the natural optics of your 3.9mm F2.8 lens keep everything from **~20.2 cm to ~32.3 cm** in sharp focus. This perfectly covers the front half of your turntable.

## 3. Focus Stacking (`enfuse`) and Meshroom

If you decide you *must* use focus stacking (e.g., for extreme macro objects closer than 10cm), you can use open-source tools like `enfuse`. However, focus stacking can easily break Meshroom if you aren't careful.

### The 3 Major Pitfalls:
1. **Artifacts & Halos (The SIFT Killer)**: `enfuse` blends images using contrast weighting. At sharp depth edges (where a near object overlaps a distant background), it often produces blurry halos or "ghost" double-edges. Meshroom's SIFT feature detector will latch onto these fake blurry halos, causing the 3D math to fail or produce floating artifacts.
2. **Focus Breathing Requires Pre-Alignment**: When your VCM changes focus, the lens "breathes" (zooms slightly in/out). `enfuse` does NOT align images. You **must** run `align_image_stack -m` first to compensate for the optical scale difference.
3. **EXIF Stripping**: `enfuse` outputs an image with zero metadata. It wipes out the `Make`, `Model`, `FocalLength`, and our custom `BodySerialNumber`. You must use `exiftool` to copy the EXIF back onto the fused image before giving it to Meshroom.

### Recommended `enfuse` Settings:
If you must use it, prioritize contrast and use a hard mask to reduce blurry halos:
```bash
# 1. Align images (the -m flag optimizes field of view differences)
align_image_stack -m -a aligned_ img1.jpg img2.jpg

# 2. Enfuse (optimized for focus stacking, disabled HDR/exposure fusion)
enfuse --exposure-weight=0 --saturation-weight=0 --contrast-weight=1 --hard-mask -o fused.jpg aligned_*.tif
```

### Python Alternatives to Enfuse
If you want to automate focus stacking directly inside your Python script without relying on external bash commands, there are better native options:

1. **The `focus-stack` library**: 
   You can install this open-source package via `pip install focus-stack`. It uses OpenCV under the hood to automatically align images (using homography) and blends them using Laplacian variance. It is much cleaner to integrate into a Python scanner pipeline.
   
2. **Custom OpenCV Pipeline**:
   If you want total control over the blending to reduce the blurry edge halos that ruin Meshroom SIFT detection, you can write a custom OpenCV script. The standard approach is to align images using `cv2.findTransformECC()`, compute the sharpness of each pixel using `cv2.Laplacian()`, and merge the sharpest pixels.

*(Note: Just like `enfuse`, OpenCV will strip your EXIF data when it saves the final stacked image. You must always use the `piexif` library to re-inject your `Make`, `Model`, `FocalLength`, and custom `BodySerialNumber` before giving the stacked photo to Meshroom).*

## 4. The Better Alternative: The "Two-Pass" Method

Instead of focus stacking or shifting the VCM back-and-forth at every turntable stop (which introduces mechanical jitter and ruins Meshroom's intrinsic calibration), do **Two Full Passes**.

1. **Pass 1 (Near)**: Set focus to your near target (e.g., VCM `300`). **Do not move the VCM.** Rotate the turntable 360° and take photos. Tag them in EXIF with `SerialNumber: cam0_near` and `cam1_near`.
2. **Pass 2 (Far)**: Set focus to your far target (e.g., VCM `700`). **Do not move the VCM.** Rotate the turntable 360° again. Tag them in EXIF with `SerialNumber: cam0_far` and `cam1_far`.
3. Feed all photos to Meshroom. 

Meshroom will see 4 distinct, highly stable cameras. It will seamlessly find features on the sharp foreground from Pass 1 and the sharp background from Pass 2, tying the 3D model together with **zero blending halos** and perfect mathematical stability!
