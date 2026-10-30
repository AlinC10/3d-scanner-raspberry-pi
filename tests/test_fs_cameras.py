"""
First-scan camera work package (DUAL-1/2/4/9/10/11, CAM-2/3/4/9/11/18).

Everything runs against fakes: a fake Picamera2 (frames, metadata, controls),
a fake cv2 (writes a few bytes, records the array it was given) and a fake
smbus2 bus (counts I2C writes). No hardware is touched.
"""
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import hardware.dual_camera_rpi_sync as sync_mod
from hardware.base_dual_camera import BaseDualCamera, CaptureError
from hardware.camera import focuser as focuser_mod
from hardware.camera.camera import ArducamIMX477
from hardware.camera.focuser import Focuser, FocuserUnavailable, FocuserWriteError
from hardware.dual_camera_rpi_sync import DualCameraRPISync

SIZE = (40, 30)
STRIDE = 48  # wider than SIZE[0]: simulates the ISP row padding


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────
class FakeImage:
    """Minimal ndarray stand-in: a shape, copy() and 2-D slicing."""

    def __init__(self, shape):
        self.shape = tuple(shape)

    def copy(self):
        return FakeImage(self.shape)

    def __getitem__(self, key):
        rows, cols = key
        h = len(range(*rows.indices(self.shape[0])))
        w = len(range(*cols.indices(self.shape[1])))
        return FakeImage((h, w) + self.shape[2:])


class FakeCV2:
    COLOR_YUV2BGR_I420 = 1
    IMWRITE_JPEG_QUALITY = 2

    def __init__(self, payload=b"JPEG"):
        self.payload = payload
        self.written = []  # (path, shape)

    def cvtColor(self, yuv, code):
        return FakeImage((yuv.shape[0] * 2 // 3, yuv.shape[1], 3))

    def imwrite(self, path, img, params=None):
        self.written.append((path, img.shape))
        with open(path, "wb") as f:
            f.write(self.payload)
        return True

    def imencode(self, ext, img, params=None):
        return True, types.SimpleNamespace(tobytes=lambda: b"x")


class FakeRequest:
    def __init__(self, md, main_shape):
        self._md = md
        self._main_shape = main_shape

    def get_metadata(self):
        return dict(self._md)

    def make_array(self, name):
        if name == "main":
            return FakeImage(self._main_shape)
        return FakeImage((12, 16))

    def acquire(self):
        pass

    def release(self):
        pass

    def save_dng(self, path):
        pass


class FakePicam2:
    def __init__(self, events=None, tag="", exposure=8000, gain=1.5, gains=(1.2, 1.4)):
        self.started = False
        self.events = events if events is not None else []
        self.tag = tag
        self.fail_start = False
        self.fail_capture = False
        self.fail_close = False
        self.recording = False
        self.encoders = []
        self.controls = []
        self.stop_calls = 0
        self.close_calls = 0
        self.metadata = {
            "ExposureTime": exposure,
            "AnalogueGain": gain,
            "ColourGains": gains,
        }
        self._n = 0

    def create_video_configuration(self, **kw):
        return kw

    def configure(self, cfg):
        pass

    def start(self):
        if self.fail_start:
            raise RuntimeError("start failed")
        self.started = True

    def stop(self):
        self.started = False
        self.stop_calls += 1
        self.events.append(("stop", self.tag))

    def close(self):
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("close failed")

    def stop_recording(self):
        self.recording = False

    def stop_preview(self):
        pass

    def set_controls(self, ctrls):
        self.controls.append(dict(ctrls))

    def capture_metadata(self, wait=None):
        return dict(self.metadata)

    def capture_request(self, wait=None):
        if self.fail_capture:
            raise RuntimeError("no frames")
        time.sleep(0.01)
        self._n += 1
        md = dict(self.metadata)
        md.update({"SyncReady": True, "SensorTimestamp": 1_000_000_000 + self._n * 100_000_000})
        return FakeRequest(md, (SIZE[1] * 3 // 2, STRIDE))


class FakeFocuser:
    def __init__(self):
        self.position = 0
        self.invalidated = 0
        self.restored = 0
        self.closed = 0

    def invalidate(self):
        self.invalidated += 1

    def restore_state(self):
        self.restored += 1
        return True

    def set_position(self, pos, settle=True):
        self.position = pos

    def close(self):
        self.closed += 1


class FakeCam:
    """Stand-in for ArducamIMX477 as used by DualCameraRPISync / BaseDualCamera."""

    FULL_RES = (4056, 3040)
    EVENTS = None  # set per test

    def __init__(self, camera_id=0, quality=95, size=None, rotation=0, **kw):
        self.camera_id = camera_id
        self.quality = quality
        self.rotation = rotation
        events = FakeCam.EVENTS if FakeCam.EVENTS is not None else []
        self.picam2 = FakePicam2(
            events,
            tag=f"cam{camera_id + 1}",
            exposure=8000 + 1000 * camera_id,
            gain=1.5 + camera_id,
            gains=(1.2 + camera_id, 1.4 + camera_id),
        )
        self.focuser = FakeFocuser()
        self._pending_controls = {}
        self._last_metadata = None
        self.events = events
        self.apply_calls = []
        self.lock_calls = []
        self.lock_raises = False
        self.closed = 0

    def apply_settings(self, **kw):
        self.apply_calls.append(kw)

    def focus_set(self, pos):
        self.focuser.set_position(pos)

    def lock_auto_features(self, settle_time=2.0):
        self.lock_calls.append(settle_time)
        if self.lock_raises:
            raise RuntimeError("lock failed")
        md = self.picam2.capture_metadata()
        controls = {
            "AeEnable": False,
            "AwbEnable": False,
            "ExposureTime": md["ExposureTime"],
            "AnalogueGain": md["AnalogueGain"],
            "ColourGains": md["ColourGains"],
        }
        self.picam2.set_controls(controls)
        self._pending_controls.update(controls)
        return controls

    def _process_captured_image(self, path, resolution, quality):
        pass

    def close(self):
        self.closed += 1
        self.events.append(("close", f"cam{self.camera_id + 1}"))


def make_sync(ready_timeout=2.0, **kw):
    """DualCameraRPISync wired to FakeCam in real (picamera2 available) mode."""
    FakeCam.EVENTS = []
    kw.setdefault("size", SIZE)
    kw.setdefault("fps", 10)
    return DualCameraRPISync(ready_timeout=ready_timeout, auto_start=False, **kw)


# The fake cameras deliver tiny 40x30 frames to keep the tests fast, which is not a size the real driver accepts
ANY_PHOTO_SIZE = patch.object(DualCameraRPISync, "supported_photo_sizes", None)


class RealModeCase(unittest.TestCase):
    """Common patches: picamera2 'available', FakeCam as camera class, fake cv2."""

    payload = b"JPEG"

    def setUp(self):
        self.cv2 = FakeCV2(self.payload)
        for p in (
            ANY_PHOTO_SIZE,
            patch.object(sync_mod, "ArducamIMX477", FakeCam),
            patch.object(sync_mod, "PICAMERA2_AVAILABLE", True),
            patch.object(sync_mod, "OPENCV_AVAILABLE", True),
            patch.object(sync_mod, "cv2", self.cv2),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def new_driver(self, **kw):
        drv = make_sync(**kw)
        self.addCleanup(drv.close)
        return drv


# ─────────────────────────────────────────────────────────────────────────────
# Contract 1-3, 6: CaptureError, last_capture_info, stride crop
# ─────────────────────────────────────────────────────────────────────────────
class TestCapturePhoto(RealModeCase):
    def test_capture_error_is_runtime_error(self):
        self.assertTrue(issubclass(CaptureError, RuntimeError))
        drv = self.new_driver()
        self.assertIsInstance(drv, BaseDualCamera)
        self.assertIsNone(drv.last_capture_info)

    def test_successful_capture_info_shape_and_stride_crop(self):
        drv = self.new_driver()
        paths = drv.capture_photo(output_prefix="shot", output_dir=self.tmp, sync_timeout=1.0)
        self.assertEqual(len(paths), 2)
        for p in paths:
            self.assertGreater(os.path.getsize(p), 0)

        # Stride padding never reaches the encoder: both writes got width == SIZE[0]
        self.assertEqual(len(self.cv2.written), 2)
        for _path, shape in self.cv2.written:
            self.assertEqual(shape[:2], (SIZE[1], SIZE[0]))

        info = drv.last_capture_info
        self.assertEqual(info["paths"], paths)
        self.assertEqual(len(info["cameras"]), 2)
        keys = {"exposure_time", "analogue_gain", "colour_gains", "sensor_timestamp", "width", "height"}
        for idx, cam_info in enumerate(info["cameras"]):
            self.assertEqual(set(cam_info), keys)
            self.assertEqual(cam_info["exposure_time"], 8000 + 1000 * idx)
            self.assertEqual(cam_info["analogue_gain"], 1.5 + idx)
            self.assertEqual(cam_info["colour_gains"], [1.2 + idx, 1.4 + idx])
            self.assertIsInstance(cam_info["sensor_timestamp"], int)
            self.assertEqual((cam_info["width"], cam_info["height"]), SIZE)

    def test_no_crop_when_stride_equals_width(self):
        drv = self.new_driver()
        for cam in drv.cameras:
            orig = cam.picam2.capture_request

            def narrow(orig=orig, wait=None):
                req = orig(wait=wait)
                req._main_shape = (SIZE[1] * 3 // 2, SIZE[0])
                return req

            cam.picam2.capture_request = narrow
        drv.capture_photo(output_prefix="shot", output_dir=self.tmp, sync_timeout=1.0)
        self.assertTrue(all(shape[1] == SIZE[0] for _p, shape in self.cv2.written))

    def test_missing_frame_raises_capture_error(self):
        drv = self.new_driver()
        drv.cam2.picam2.fail_capture = True  # cam2 never delivers a frame
        t0 = time.monotonic()
        with self.assertRaises(CaptureError):
            drv.capture_photo(output_prefix="shot", output_dir=self.tmp, sync_timeout=0.05)
        self.assertLess(time.monotonic() - t0, 6.0)
        self.assertIsNone(drv.last_capture_info)

    def test_zero_byte_file_raises_capture_error(self):
        self.cv2.payload = b""
        drv = self.new_driver()
        with self.assertRaises(CaptureError):
            drv.capture_photo(output_prefix="shot", output_dir=self.tmp, sync_timeout=1.0)
        self.assertIsNone(drv.last_capture_info)

    def test_stale_file_does_not_mask_failed_encode(self):
        drv = self.new_driver()
        paths = drv.capture_photo(output_prefix="shot", output_dir=self.tmp, sync_timeout=1.0)
        self.assertTrue(all(os.path.getsize(p) > 0 for p in paths))
        self.cv2.payload = b""  # second capture to the same paths now writes empty files
        with self.assertRaises(CaptureError):
            drv.capture_photo(output_prefix="shot", output_dir=self.tmp, sync_timeout=1.0)

    def test_closed_driver_raises_in_real_mode(self):
        drv = self.new_driver()
        drv.close()
        with self.assertRaises(CaptureError):
            drv.capture_photo(output_prefix="shot", output_dir=self.tmp)


class TestSupportedPhotoSizes(unittest.TestCase):
    def build(self, size):
        FakeCam.EVENTS = []
        with patch.object(sync_mod, "ArducamIMX477", FakeCam), patch.object(sync_mod, "PICAMERA2_AVAILABLE", False):
            drv = DualCameraRPISync(size=size, auto_start=False)
        self.addCleanup(drv.close)
        return drv

    def test_the_two_native_sizes_are_accepted(self):
        for size in ((4056, 3040), (2028, 1520)):
            self.assertEqual(tuple(self.build(size).size), size)

    def test_any_other_size_is_refused_before_a_camera_is_touched(self):
        for size in ((640, 480), (1920, 1080), (4056, 3039), (3040, 4056)):
            with self.assertRaises(ValueError):
                self.build(size)

    def test_the_default_size_is_a_supported_one(self):
        self.assertIn(tuple(self.build(None).size), DualCameraRPISync.supported_photo_sizes)

    def test_the_sensor_is_configured_at_the_requested_size_so_the_binned_mode_keeps_the_field_of_view(self):
        self.assertEqual(DualCameraRPISync.supported_photo_sizes, ((4056, 3040), (2028, 1520)))


class TestStubMode(unittest.TestCase):
    def test_stub_mode_writes_placeholders_and_does_not_raise(self):
        FakeCam.EVENTS = []
        with ANY_PHOTO_SIZE, patch.object(sync_mod, "ArducamIMX477", FakeCam), \
                patch.object(sync_mod, "PICAMERA2_AVAILABLE", False):
            drv = DualCameraRPISync(size=SIZE, auto_start=False)
            self.addCleanup(drv.close)
            with tempfile.TemporaryDirectory() as tmp:
                paths = drv.capture_photo(output_prefix="stub", output_dir=tmp)
                self.assertEqual(len(paths), 2)
                for p in paths:
                    self.assertTrue(os.path.exists(p))
                info = drv.last_capture_info
                self.assertEqual(info["paths"], paths)
                self.assertEqual(len(info["cameras"]), 2)
                self.assertIsNone(info["cameras"][0]["exposure_time"])


# ─────────────────────────────────────────────────────────────────────────────
# Contract 4: lock_auto
# ─────────────────────────────────────────────────────────────────────────────
class TestPrepareScanLock(RealModeCase):
    def test_lock_auto_keeps_the_own_colour_gains_of_cam2_by_default(self):
        drv = self.new_driver()
        infos = drv.prepare_scan(lock_auto=True, settle_time=0, exposure_time=None)
        self.assertTrue(infos[0]["locked"] and infos[1]["locked"])
        # exposure and gain are shared (cam1's values) ...
        for info in infos:
            self.assertEqual(info["exposure_time"], 8000)
            self.assertEqual(info["analogue_gain"], 1.5)
        # ... the white balance is not: each camera keeps the gains its own AWB settled on
        self.assertEqual(infos[0]["colour_gains"], [1.2, 1.4])
        self.assertEqual(infos[1]["colour_gains"], [2.2, 2.4])
        last = drv.cam2.picam2.controls[-1]
        self.assertEqual(last["ExposureTime"], 8000)
        self.assertEqual(last["AnalogueGain"], 1.5)
        self.assertNotEqual(last.get("ColourGains"), (1.2, 1.4))
        self.assertFalse(last["AeEnable"])

    def test_lock_auto_true_locks_and_copies_to_cam2(self):
        drv = self.new_driver()
        infos = drv.prepare_scan(lock_auto=True, settle_time=0, exposure_time=None, copy_colour_gains=True)
        self.assertEqual(drv.cam1.lock_calls, [0.0])
        self.assertEqual(drv.cam2.lock_calls, [0.0])
        self.assertTrue(infos[0]["locked"] and infos[1]["locked"])
        for info in infos:
            self.assertEqual(info["exposure_time"], 8000)  # cam1's value
            self.assertEqual(info["analogue_gain"], 1.5)
            self.assertEqual(info["colour_gains"], [1.2, 1.4])
        last = drv.cam2.picam2.controls[-1]
        self.assertEqual(last["ExposureTime"], 8000)
        self.assertEqual(last["AnalogueGain"], 1.5)
        self.assertEqual(last["ColourGains"], (1.2, 1.4))
        self.assertFalse(last["AeEnable"])
        self.assertFalse(last["AwbEnable"])

    def test_lock_auto_rearms_ae_awb_before_locking(self):
        drv = self.new_driver()
        drv.prepare_scan(lock_auto=True, settle_time=0)
        first = drv.cam1.picam2.controls[0]
        self.assertEqual(first, {"AeEnable": True, "AwbEnable": True})

    def test_lock_auto_default_false_does_not_lock(self):
        drv = self.new_driver()
        infos = drv.prepare_scan()
        self.assertEqual(drv.cam1.lock_calls, [])
        self.assertEqual(drv.cam2.lock_calls, [])
        for info in infos:
            self.assertIs(info["locked"], False)
            self.assertIsNone(info["exposure_time"])
            self.assertIn("analogue_gain", info)
            self.assertIn("colour_gains", info)

    def test_lock_auto_does_not_leak_into_camera_kwargs(self):
        drv = self.new_driver()
        drv.prepare_scan(lock_auto=True, settle_time=0, exposure_time=5000, brightness=0.1)
        for cam in drv.cameras:
            self.assertEqual(len(cam.apply_calls), 1)
            self.assertNotIn("lock_auto", cam.apply_calls[0])
            self.assertEqual(cam.apply_calls[0], {"exposure_time": 5000, "brightness": 0.1})
        # manual exposure -> AE is not re-armed
        self.assertNotIn("AeEnable", drv.cam1.picam2.controls[0])

    def test_lock_failure_is_reported_not_raised(self):
        drv = self.new_driver()
        drv.cam1.lock_raises = True
        infos = drv.prepare_scan(lock_auto=True, settle_time=0)
        self.assertFalse(infos[0]["locked"])
        self.assertFalse(infos[1]["locked"])

    def test_ready_waits_share_one_deadline(self):
        drv = self.new_driver(ready_timeout=0.4)
        drv.cam1.picam2.fail_capture = True
        drv.cam2.picam2.fail_capture = True
        t0 = time.monotonic()
        infos = drv.prepare_scan()
        self.assertLess(time.monotonic() - t0, 0.75)  # was 2 x ready_timeout
        self.assertFalse(infos[0]["ready"])

    def test_focus_restored_when_not_given(self):
        drv = self.new_driver()
        drv.prepare_scan()
        self.assertEqual(drv.cam1.focuser.restored, 1)
        self.assertEqual(drv.cam2.focuser.restored, 1)
        drv.prepare_scan(focus=120)
        self.assertEqual(drv.cam1.focuser.position, 120)


class TestAutofocus(RealModeCase):
    """focus='auto': a sweep over the running pipeline in the in-position call; the best position stays locked."""

    PEAK = 640

    def setUp(self):
        super().setUp()
        self.drv = None
        outer = self

        def sharpness(frame, roi):
            # Sharpest when cam1's lens is at PEAK (both lenses visit the same positions in these tests)
            return -abs(outer.drv.cam1.focuser.position - outer.PEAK)

        p = patch.object(FakeCam, "_sharpness", create=True, new=staticmethod(sharpness))
        p.start()
        self.addCleanup(p.stop)

    def driver(self):
        self.drv = self.new_driver()
        return self.drv

    def test_auto_in_the_lock_call_sweeps_and_leaves_the_best_position(self):
        drv = self.driver()
        infos = drv.prepare_scan(focus="auto", lock_auto=True, settle_time=0)
        # coarse step 30 -> 630, fine pass (step 6) -> 642 is the closest to the peak
        self.assertEqual(drv.autofocus_positions, [642, 642])
        self.assertEqual(drv.cam1.focuser.position, 642)
        self.assertEqual(drv.cam2.focuser.position, 642)
        self.assertTrue(infos[0]["autofocus"] and infos[1]["autofocus"])
        self.assertEqual(infos[0]["focus"], 642)

    def test_auto_in_the_first_call_only_restores_the_saved_position(self):
        drv = self.driver()
        infos = drv.prepare_scan(focus="auto")
        self.assertIsNone(drv.autofocus_positions)
        self.assertEqual(drv.cam1.focuser.restored, 1)
        self.assertFalse(infos[0]["autofocus"])

    def test_focus_stays_locked_after_the_sweep(self):
        drv = self.driver()
        drv.prepare_scan(focus="auto", lock_auto=True, settle_time=0)
        drv.prepare_scan()                      # a later call without focus must not move the lens
        self.assertEqual(drv.cam1.focuser.position, 642)

    def test_sweep_needs_the_stream(self):
        drv = self.driver()
        with self.assertRaises(RuntimeError):
            drv.focus_sweep_autofocus()

    def test_unknown_focus_word_is_refused(self):
        drv = self.driver()
        with self.assertRaises(ValueError):
            drv.prepare_scan(focus="near")

    def test_coarse_sweep_stops_right_after_the_peak(self):
        drv = self.driver()
        visited = []
        original = FakeFocuser.set_position

        def record(focuser, pos, settle=True):
            if focuser is drv.cam1.focuser:
                visited.append(pos)
            original(focuser, pos, settle)

        with patch.object(FakeFocuser, "set_position", record):
            drv.prepare_scan(focus="auto", lock_auto=True, settle_time=0)
        # coarse 0, 30, ..., 630 (best), 660 (first drop) -> stop; then 11 fine positions and the final set
        coarse = visited[:visited.index(660) + 1]
        self.assertEqual(coarse, list(range(0, 661, 30)))
        self.assertEqual(len(visited), len(coarse) + 11 + 1)
        self.assertEqual(drv.autofocus_positions, [642, 642])

    def test_each_lens_stops_at_its_own_peak(self):
        drv = self.driver()
        peaks = {0.0: 300, 0.5: 900}            # cam1 box starts at x=0, cam2 box at x=0.5
        cams = {0.0: drv.cam1, 0.5: drv.cam2}

        def sharpness(frame, roi):
            return 2000 - abs(cams[roi[0]].focuser.position - peaks[roi[0]])

        with patch.object(FakeCam, "_sharpness", create=True, new=staticmethod(sharpness)):
            drv.prepare_scan(focus="auto", lock_auto=True, settle_time=0,
                             autofocus_roi=((0.0, 0.3, 0.4, 0.4), (0.5, 0.3, 0.4, 0.4)))
        self.assertEqual(drv.autofocus_positions, [300, 900])

    def test_peak_passed_needs_a_rise_before_a_drop(self):
        passed = self.driver()._peak_passed
        self.assertFalse(passed({0: 100.0, 30: 99.0}))               # flat blurred start: noise, not a peak
        self.assertFalse(passed({0: 100.0, 30: 150.0, 60: 149.0}))   # within the tolerance
        self.assertTrue(passed({0: 100.0, 30: 150.0, 60: 140.0}))
        self.assertFalse(passed({0: 100.0, 30: 150.0, 60: 160.0}))   # still rising

    def test_sweep_frame_collection_is_switched_off_afterwards(self):
        drv = self.driver()
        drv.prepare_scan(focus="auto", lock_auto=True, settle_time=0)
        self.assertEqual(drv._af_active, [False, False])
        self.assertEqual(drv._af_frame, [None, None])


# ─────────────────────────────────────────────────────────────────────────────
# Contract 7: lifecycle
# ─────────────────────────────────────────────────────────────────────────────
class TestPipelineLifecycle(RealModeCase):
    def test_start_failure_rolls_back(self):
        drv = self.new_driver()
        drv.cam1.picam2.fail_start = True  # cam2 starts first, then cam1 fails
        with self.assertRaises(RuntimeError):
            drv._start_pipeline()
        self.assertFalse(drv._running)
        self.assertFalse(drv._is_streaming)
        self.assertEqual(drv.cam2.picam2.stop_calls, 1)  # what was started is stopped
        self.assertFalse(drv.cam2.picam2.started)
        self.assertEqual(drv._threads, [])
        self.assertGreaterEqual(drv.cam2.focuser.invalidated, 1)
        # A later attempt is not poisoned by stale state
        drv.cam1.picam2.fail_start = False
        drv._start_pipeline()
        self.assertTrue(drv.is_streaming)

    def test_close_stops_stream_first_and_is_idempotent(self):
        drv = make_sync()
        drv._start_pipeline()
        events = FakeCam.EVENTS
        cam1, cam2 = drv.cam1, drv.cam2
        drv.close()
        drv.close()
        self.assertEqual(cam1.closed, 1)
        self.assertEqual(cam2.closed, 1)
        first_close = min(i for i, e in enumerate(events) if e[0] == "close")
        last_stop = max(i for i, e in enumerate(events) if e[0] == "stop")
        self.assertLess(last_stop, first_close)
        self.assertFalse(drv.is_streaming)

    def test_closed_driver_refuses_to_restart(self):
        drv = make_sync()
        drv.close()
        with self.assertRaises(RuntimeError):
            drv._start_pipeline()
        self.assertEqual(drv.cam1.picam2.started, False)
        with self.assertRaises(RuntimeError):
            drv.start_stream()

    def test_stop_stream_invalidates_focus(self):
        drv = self.new_driver()
        drv._start_pipeline()
        before = drv.cam1.focuser.invalidated
        drv.stop_stream()
        self.assertGreater(drv.cam1.focuser.invalidated, before)
        self.assertGreater(drv.cam2.focuser.invalidated, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Real ArducamIMX477 on a fake picam2 / fake I2C bus
# ─────────────────────────────────────────────────────────────────────────────
class FakeBus:
    def __init__(self):
        self.writes = []
        self.init_writes = 0
        self.fail = False
        self.fail_next = 0
        self.closed = 0
        self.close_raises = False

    def write_byte_data(self, addr, reg, val):
        self.init_writes += 1

    def write_i2c_block_data(self, addr, reg, data):
        if self.fail or self.fail_next > 0:
            self.fail_next = max(0, self.fail_next - 1)
            self.writes.append(("fail", reg, tuple(data)))
            raise OSError(121, "Remote I/O error")
        self.writes.append(("ok", reg, tuple(data)))

    def close(self):
        self.closed += 1
        if self.close_raises:
            raise OSError("close")

    def ok_values(self):
        return [((reg << 4) | (data[0] >> 4)) for kind, reg, data in self.writes if kind == "ok"]


def make_real_camera(tmp, camera_id=0):
    cam = ArducamIMX477(camera_id=camera_id)
    cam.picam2 = MagicMock()
    cam.picam2.started = True
    cam.picam2.recording = False
    cam.picam2.encoders = []
    cam.focuser._bus = FakeBus()
    cam.focuser._state_file = type(cam.focuser._state_file)(os.path.join(tmp, "focus.json"))
    return cam


class TestApplySettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.cam = make_real_camera(self.tmp)

    def applied(self):
        return self.cam.picam2.set_controls.call_args[0][0]

    def test_brightness_only_does_not_touch_ae(self):
        self.cam.apply_settings(brightness=0.1, contrast=1.2)
        ctls = self.applied()
        self.assertEqual(ctls, {"Brightness": 0.1, "Contrast": 1.2})

    def test_exposure_disables_ae(self):
        self.cam.apply_settings(exposure_time=12000)
        self.assertEqual(self.applied(), {"ExposureTime": 12000, "AeEnable": False})

    def test_awb_preset_keeps_awb_enabled(self):
        self.cam.apply_settings(awb_mode="daylight")
        ctls = self.applied()
        self.assertIn("AwbMode", ctls)
        self.assertIs(ctls["AwbEnable"], True)
        self.assertNotIn("AeEnable", ctls)

    def test_manual_colour_gains_disable_awb(self):
        self.cam.apply_settings(colour_gains=(1.5, 1.7))
        self.assertEqual(self.applied(), {"ColourGains": (1.5, 1.7), "AwbEnable": False})

    def test_gain_only_locks_exposure_from_metadata(self):
        self.cam._last_metadata = {"ExposureTime": 7000}
        self.cam.apply_settings(analogue_gain=2.0)
        self.assertEqual(self.applied(), {"AnalogueGain": 2.0, "ExposureTime": 7000, "AeEnable": False})

    def test_gain_only_without_metadata_leaves_ae(self):
        self.cam.apply_settings(analogue_gain=2.0)
        self.assertEqual(self.applied(), {"AnalogueGain": 2.0})


class TestCameraClose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.cam = make_real_camera(self.tmp)
        self.bus = self.cam.focuser._bus

    def test_close_releases_picam2_and_bus_and_is_idempotent(self):
        self.cam.picam2.recording = True
        self.cam.close()
        self.cam.close()
        self.cam.picam2.stop_recording.assert_called_once()
        self.cam.picam2.stop_preview.assert_called_once()
        self.cam.picam2.stop.assert_called_once()
        self.cam.picam2.close.assert_called_once()
        self.assertEqual(self.bus.closed, 1)

    def test_focuser_closed_even_when_picam2_raises(self):
        self.cam.picam2.stop.side_effect = RuntimeError("stop")
        self.cam.picam2.stop_preview.side_effect = RuntimeError("preview")
        self.cam.picam2.close.side_effect = RuntimeError("close")
        self.cam.close()  # must not raise
        self.assertEqual(self.bus.closed, 1)
        self.cam.picam2.close.assert_called_once()

    def test_prepare_scan_forces_focus_write_and_invalidates_on_stop(self):
        f = self.cam.focuser
        f._position = 300  # cached from the state file after a reboot; VCM is physically at 0
        self.cam.prepare_scan(
            focus=300, settle_time=0, exposure_time=1000, analogue_gain=1.0, colour_gains=(1.0, 1.0)
        )
        values = self.bus.ok_values()
        self.assertTrue(values, "no I2C write although the lens may be at 0 after reboot")
        self.assertEqual(values[-1], 300)
        self.assertFalse(f.hw_valid)  # camera stopped -> VCM unpowered


# ─────────────────────────────────────────────────────────────────────────────
# Contract 8: Focuser
# ─────────────────────────────────────────────────────────────────────────────
class TestFocuser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.bus = FakeBus()
        fake_smbus = types.SimpleNamespace(SMBus=lambda n: self.bus)
        p = patch.object(focuser_mod, "smbus2", fake_smbus)
        p.start()
        self.addCleanup(p.stop)
        self.f = Focuser(bus=97)
        self.f._state_file = type(self.f._state_file)(os.path.join(self.tmp, "state.json"))
        self.f._position = 0

    def test_first_write_forced_even_when_cached_equals_target(self):
        self.f.set_position(0, settle=False)
        self.assertEqual(len(self.bus.writes), 1)  # cached 0 == target 0, still physically written
        n = len(self.bus.writes)
        self.f.set_position(0, settle=False)
        self.assertEqual(len(self.bus.writes), n)  # hardware known to be there: no-op

    def test_invalidate_forces_next_write_and_reinit(self):
        self.f.set_position(150, settle=False)
        n, inits = len(self.bus.writes), self.bus.init_writes
        self.f.set_position(150, settle=False)
        self.assertEqual(len(self.bus.writes), n)
        self.f.invalidate()
        self.f.set_position(150, settle=False)
        self.assertGreater(len(self.bus.writes), n)
        self.assertEqual(self.bus.ok_values()[-1], 150)
        self.assertEqual(self.bus.init_writes, inits + 1)

    def test_mark_unpowered_alias(self):
        self.f.set_position(40, settle=False)
        self.assertTrue(self.f.hw_valid)
        self.f.mark_unpowered()
        self.assertFalse(self.f.hw_valid)

    def test_cached_position_ramps_from_zero_after_power_cycle(self):
        self.f._position = 100
        self.f.set_position(100, settle=False)
        values = self.bus.ok_values()
        self.assertEqual(values[0], 0)
        self.assertEqual(values[-1], 100)
        self.assertTrue(all(b - a <= 20 for a, b in zip(values, values[1:])))

    def test_failed_write_does_not_commit_position_or_state(self):
        self.bus.fail = True
        with self.assertRaises(FocuserUnavailable):
            self.f.set_position(200, settle=False)
        self.assertEqual(self.f.position, 0)
        self.assertFalse(os.path.exists(self.f._state_file))
        self.assertFalse(self.f.hw_valid)
        self.assertEqual(len(self.bus.writes), 2)  # original attempt + exactly one retry
        # recovers once the bus works again, and the move is really performed
        self.bus.fail = False
        self.f.set_position(200, settle=False)
        self.assertEqual(self.f.position, 200)
        self.assertTrue(os.path.exists(self.f._state_file))

    def test_single_transient_failure_is_retried(self):
        self.bus.fail_next = 1
        self.f.set_position(10, settle=False)
        self.assertEqual(self.f.position, 10)
        self.assertEqual(self.bus.ok_values()[-1], 10)

    def test_write_error_is_a_runtime_error_subclass(self):
        self.assertTrue(issubclass(FocuserWriteError, FocuserUnavailable))
        self.assertTrue(issubclass(FocuserUnavailable, RuntimeError))

    def test_restore_state_reports_failure_without_raising(self):
        self.f._position = 80
        self.bus.fail = True
        self.assertFalse(self.f.restore_state())
        self.bus.fail = False
        self.assertTrue(self.f.restore_state())
        self.assertEqual(self.bus.ok_values()[-1], 80)

    def test_close_is_idempotent(self):
        self.f.close()
        self.f.close()
        self.assertEqual(self.bus.closed, 1)
        with self.assertRaises(FocuserUnavailable):
            self.f.set_position(5, settle=False)

    def test_missing_i2c_bus_raises_focuser_unavailable_not_systemexit(self):
        def boom(n):
            raise FileNotFoundError("no i2c")

        with patch.object(focuser_mod, "smbus2", types.SimpleNamespace(SMBus=boom)):
            with self.assertRaises(FocuserUnavailable):
                Focuser(bus=96)

    def test_missing_smbus2_raises_focuser_unavailable_not_systemexit(self):
        with patch.object(focuser_mod, "smbus2", None):
            with self.assertRaises(FocuserUnavailable):
                Focuser(bus=95)


if __name__ == "__main__":
    unittest.main()
