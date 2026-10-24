import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hardware.base_dual_camera import BaseDualCamera, DEFAULT_STREAM_SIZE
from hardware.dual_camera import DualCamera
from hardware.dual_camera_rpi_sync import DualCameraRPISync
from hardware.dual_camera_xvs import DualCameraXVS

# Everything the scanner and the routers use on `scanner.dual_cameras`: it must exist on every driver
INTERFACE = (
    "supports_preview_stream", "supports_lock_auto",
    "stream_size", "stream_quality", "autofocus_positions", "last_capture_info",
    "active_camera", "active", "add_spectator", "remove_spectator", "has_spectators", "wait_jpeg", "get_sync_status",
    "get_latest_lores_frames",
    "prepare_scan", "start_stream", "stop_stream", "capture_photo", "record_video", "close",
    "focus_set", "focus_step", "focus_reset", "focus_sweep_autofocus", "lock_auto_features", "apply_settings",
)


class _Driver(BaseDualCamera):
    """The smallest concrete driver: only the abstract methods, nothing else."""

    def prepare_scan(self, *, lock_auto=False, **kwargs):
        return []

    def start_stream(self, bitrate=4_000_000):
        return []

    def stop_stream(self):
        pass

    def capture_photo(self, output_prefix="dual_photo", output_dir=".", meshroom_rig=True, **kwargs):
        return []

    def record_video(self, output_prefix="dual_video", duration=10.0, **kwargs):
        return []


class _Restricted(_Driver):
    """A driver that supports only the two native photo sizes."""
    supported_photo_sizes = ((4056, 3040), (2028, 1520))


class InterfaceDeclaredInTheBaseTest(unittest.TestCase):
    def test_every_driver_has_every_interface_member(self):
        # stream_size & co. are instance attributes set by BaseDualCamera.__init__, the rest are class members
        instance_attributes = {"stream_size", "stream_quality", "autofocus_positions", "last_capture_info"}
        for cls in (DualCameraRPISync, DualCamera, DualCameraXVS):
            for name in INTERFACE:
                if name in instance_attributes and not hasattr(cls, name):
                    self.assertIn(name, inspect.getsource(BaseDualCamera), f"{cls.__name__}.{name}")
                    continue
                self.assertTrue(hasattr(cls, name), f"{cls.__name__} lacks {name}")

    def test_interface_members_that_only_some_drivers_implement_live_in_the_base(self):
        for name in ("active_camera", "active", "add_spectator", "remove_spectator", "has_spectators", "wait_jpeg",
                     "get_sync_status", "stream_quality"):
            self.assertIn(name, vars(BaseDualCamera), f"{name} is not declared in BaseDualCamera")

    def test_prepare_scan_of_every_driver_accepts_lock_auto(self):
        for cls in (BaseDualCamera, DualCameraRPISync, DualCamera, DualCameraXVS):
            self.assertIn("lock_auto", inspect.signature(cls.prepare_scan).parameters, cls.__name__)

    def test_prepare_scan_of_every_driver_accepts_copy_colour_gains(self):
        for cls in (BaseDualCamera, DualCameraRPISync, DualCamera, DualCameraXVS):
            self.assertIn("copy_colour_gains", inspect.signature(cls.prepare_scan).parameters, cls.__name__)

    def test_only_the_sync_driver_restricts_the_photo_sizes(self):
        self.assertEqual(DualCameraRPISync.supported_photo_sizes, ((4056, 3040), (2028, 1520)))
        for cls in (BaseDualCamera, DualCamera, DualCameraXVS):
            self.assertIsNone(cls.supported_photo_sizes, cls.__name__)

    def test_a_restricted_driver_refuses_other_sizes_in_the_base_constructor(self):
        _Restricted(size=(2028, 1520))
        _Restricted(size=[4056, 3040])
        _Restricted()  # the default size is one of them
        for size in ((640, 480), (1920, 1080)):
            with self.assertRaises(ValueError):
                _Restricted(size=size)
        _Driver(size=(640, 480))  # a driver that does not restrict keeps accepting any size

    def test_capability_flags(self):
        self.assertTrue(DualCameraRPISync.supports_preview_stream)
        self.assertTrue(DualCameraRPISync.supports_lock_auto)
        for cls in (BaseDualCamera, DualCamera, DualCameraXVS):
            self.assertFalse(cls.supports_preview_stream, cls.__name__)
            self.assertFalse(cls.supports_lock_auto, cls.__name__)

    def test_secondary_drivers_do_not_override_the_shared_preview_members(self):
        for cls in (DualCamera, DualCameraXVS):
            for name in ("active_camera", "active", "add_spectator", "wait_jpeg", "get_sync_status", "stream_quality"):
                self.assertNotIn(name, vars(cls), f"{cls.__name__} redefines {name}")


class SharedBehaviourTest(unittest.TestCase):
    def setUp(self):
        self.d = _Driver()

    def test_stream_defaults_and_parameters(self):
        self.assertEqual(self.d.stream_size, DEFAULT_STREAM_SIZE)
        self.assertEqual(self.d.stream_quality, 80)
        d = _Driver(stream_size=[640, 480], stream_quality=60)
        self.assertEqual(d.stream_size, (640, 480))
        self.assertEqual(d.stream_quality, 60)
        self.assertIsNone(d.autofocus_positions)

    def test_stream_quality_is_clamped(self):
        self.d.stream_quality = 500
        self.assertEqual(self.d.stream_quality, 100)
        self.d.stream_quality = -3
        self.assertEqual(self.d.stream_quality, 1)

    def test_active_camera_is_one_indexed_and_active_is_zero_indexed(self):
        self.assertEqual((self.d.active_camera, self.d.active), (1, 0))
        self.d.active_camera = 2
        self.assertEqual((self.d.active_camera, self.d.active), (2, 1))
        self.d.active = 0
        self.assertEqual(self.d.active_camera, 1)
        with self.assertRaises(ValueError):
            self.d.active_camera = 3
        with self.assertRaises(ValueError):
            self.d.active = 2

    def test_spectators_are_counted_per_camera(self):
        self.assertFalse(self.d.has_spectators())
        self.d.add_spectator(0)
        self.d.add_spectator(2)      # 1-indexed camera 2 == idx 1
        self.d.add_spectator(2)
        self.assertTrue(self.d.has_spectators(0))
        self.assertTrue(self.d.has_spectators(1))
        self.d.remove_spectator(1)
        self.assertTrue(self.d.has_spectators(1))
        self.d.remove_spectator(1)
        self.d.remove_spectator(1)   # never goes below zero
        self.assertFalse(self.d.has_spectators(1))
        self.assertTrue(self.d.has_spectators())

    def test_default_preview_and_status_for_drivers_without_a_preview(self):
        self.assertEqual(self.d.wait_jpeg(0, 7, timeout=0.0), (7, None))
        status = self.d.get_sync_status()
        self.assertEqual(status["mode"], "_Driver")
        self.assertTrue(status["ready"])
        self.assertIn("is_streaming", status)


if __name__ == "__main__":
    unittest.main()
