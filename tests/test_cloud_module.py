"""Runs tests/cloud_isolated/ (the real cloud/ module) in a clean interpreter: tests/__init__.py mocks `cloud`."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestCloudModule(unittest.TestCase):
    def test_cloud_suite(self):
        suite_dir = os.path.join(ROOT, "tests", "cloud_isolated")
        env = dict(os.environ, PYTHONPATH=ROOT, RUNPOD_API_KEY="")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", suite_dir, "-t", suite_dir, "-p", "suite_*.py"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=240,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])


if __name__ == "__main__":
    unittest.main()
