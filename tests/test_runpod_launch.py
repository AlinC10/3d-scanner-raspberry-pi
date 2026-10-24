"""
cloud/runpod.py launch_job(): the pod is always terminated (CLOUD-1) and the number of attempts is bounded, with a
last attempt on the host with the most RAM (CLOUD-2). The real module is loaded by path with a fake RunPod API and a
fake bucket: nothing here reaches the network.
"""
import importlib.util
import os
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def load_runpod(r2):
    """cloud/runpod.py with the real RAM heuristic, a fake `requests` and the given bucket module."""
    package = types.ModuleType("cloud")
    package.__path__ = [os.path.join(ROOT, "cloud")]
    spec = importlib.util.spec_from_file_location("cloud.ram_heuristic", os.path.join(ROOT, "cloud", "ram_heuristic.py"))
    ram = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ram)
    modules = {"cloud": package, "cloud.ram_heuristic": ram, "cloud.cloudflare_r2": r2, "requests": MagicMock()}
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("runpod_under_test", os.path.join(ROOT, "cloud", "runpod.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class FakeApi:
    """Scripted RunPod: every created pod follows the next entry of `pods` (a dict, see make_pod)."""

    def __init__(self, module, pods):
        self.module = module
        self.pods = list(pods)
        self.created = []        # (pod_id, gpu, env)
        self.terminated = []
        self.terminate_errors = 0
        self.create_error = None
        self.status_error = None
        self.polls = {}
        module.create_pod = self.create_pod
        module.get_pod_status = self.get_pod_status
        module.get_pod_memory_gb = lambda pod_id: self.script(pod_id).get("memory")
        module.terminate_pod = self.terminate_pod

    def script(self, pod_id):
        return self.pods[int(pod_id.split("-")[1])]

    def create_pod(self, env, gpu):
        if self.create_error is not None:
            raise self.create_error
        pod_id = f"pod-{len(self.created)}"
        self.created.append((pod_id, gpu, env))
        return pod_id

    def get_pod_status(self, pod_id):
        if self.status_error is not None:
            raise self.status_error
        script = self.script(pod_id)
        polls = self.polls[pod_id] = self.polls.get(pod_id, 0) + 1
        done = polls >= script.get("polls_until_exit", 1)
        return {"id": pod_id, "desiredStatus": "EXITED" if done else "RUNNING",
                "runtime": {"uptimeInSeconds": script.get("uptime", 600)}}

    def terminate_pod(self, pod_id):
        if self.terminate_errors > 0:
            self.terminate_errors -= 1
            raise RuntimeError("network down")
        self.terminated.append(pod_id)
        return "TERMINATED"


def make_pod(uptime=600, memory=None, polls_until_exit=1):
    return {"uptime": uptime, "memory": memory, "polls_until_exit": polls_until_exit}


class RunPodTestCase(unittest.TestCase):
    def setUp(self):
        self.r2 = types.ModuleType("cloud.cloudflare_r2")
        self.r2.file_exists_and_is_new = MagicMock(return_value=True)
        self.module = load_runpod(self.r2)
        for name, value in (("POLL_S", 0.0), ("TERMINATE_RETRY_S", 0.0), ("CREATE_RETRY_S", 0.0),
                            ("PASS_COOLDOWN_S", 0.0), ("FAILED_POD_COOLDOWN_S", 0.0)):
            patcher = patch.object(self.module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = patch.object(self.module.time, "sleep", lambda seconds: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.job = {"job_id": "t", "photo_count": 40, "resolution_mp": 12.0, "mode": "rig"}

    def api(self, pods):
        return FakeApi(self.module, pods)

    def gpus(self, api):
        return [gpu for _id, gpu, _env in api.created]


class TestPodIsAlwaysTerminated(RunPodTestCase):
    def test_a_successful_job_terminates_its_pod_once(self):
        api = self.api([make_pod()])
        self.assertTrue(self.module.launch_job(self.job))
        self.assertEqual(api.terminated, ["pod-0"])
        self.assertEqual(len(api.created), 1)

    def test_a_status_query_that_keeps_failing_ends_the_wait_and_still_terminates(self):
        api = self.api([make_pod()])
        api.status_error = RuntimeError("API down")
        run = self.module.run_pod({}, "gpu")
        self.assertEqual(run.outcome, "status_error")
        self.assertEqual(api.terminated, ["pod-0"])

    def test_an_exception_while_waiting_still_terminates(self):
        api = self.api([make_pod()])
        api.status_error = KeyboardInterrupt()   # not an Exception: it must not skip the cleanup
        with self.assertRaises(KeyboardInterrupt):
            self.module.run_pod({}, "gpu")
        self.assertEqual(api.terminated, ["pod-0"])

    def test_a_cancel_terminates_the_pod_and_ends_the_job(self):
        api = self.api([make_pod(polls_until_exit=1000)])
        cancel = threading.Event()
        cancel.set()
        # already cancelled before the first pod: nothing is created
        self.assertFalse(self.module.launch_job(self.job, cancel_event=cancel))
        self.assertEqual(api.created, [])
        # cancelled while the pod runs
        cancel = MagicMock()
        cancel.is_set.return_value = False
        cancel.wait.return_value = True
        self.assertFalse(self.module.launch_job(self.job, cancel_event=cancel))
        self.assertEqual(api.terminated, ["pod-0"])

    def test_a_pod_that_runs_too_long_is_terminated_and_not_retried(self):
        api = self.api([make_pod(polls_until_exit=10 ** 6)] * 3)
        with patch.object(self.module, "POD_MAX_RUNTIME_S", -1.0):
            self.assertFalse(self.module.launch_job(self.job))
        self.assertEqual(api.terminated, ["pod-0"])
        self.assertEqual(len(api.created), 1)

    def test_terminate_is_retried_until_it_works(self):
        api = self.api([make_pod()])
        api.terminate_errors = 2
        self.assertTrue(self.module.launch_job(self.job))
        self.assertEqual(api.terminated, ["pod-0"])

    def test_when_a_pod_cannot_be_terminated_no_other_pod_is_started(self):
        api = self.api([make_pod(uptime=5), make_pod(), make_pod()])
        api.terminate_errors = 10 ** 6
        self.assertFalse(self.module.launch_job(self.job))
        self.assertEqual(len(api.created), 1)
        self.assertEqual(api.terminated, [])

    def test_a_pod_that_cannot_be_created_leaves_nothing_to_terminate(self):
        api = self.api([])
        api.create_error = RuntimeError("no capacity")
        with self.assertRaises(RuntimeError):
            self.module.run_pod({}, "gpu")
        self.assertEqual(api.terminated, [])

    def test_an_unreadable_bucket_is_a_failed_attempt_not_a_crash(self):
        self.r2.file_exists_and_is_new.side_effect = TimeoutError("head_object timed out")
        api = self.api([make_pod(), make_pod(), make_pod()])
        self.assertFalse(self.module.launch_job(self.job))
        self.assertEqual(sorted(api.terminated), ["pod-0", "pod-1", "pod-2"])


class TestAttemptsAreBounded(RunPodTestCase):
    def test_a_second_pod_that_works_ends_the_job(self):
        api = self.api([make_pod(uptime=30), make_pod()])
        self.assertTrue(self.module.launch_job(self.job))
        self.assertEqual(len(api.created), 2)
        self.assertEqual(api.terminated, ["pod-0", "pod-1"])

    def test_pods_that_fail_are_limited_and_the_last_one_goes_to_the_host_with_the_most_ram(self):
        api = self.api([
            make_pod(uptime=30, memory=48), make_pod(uptime=30, memory=128), make_pod(uptime=30, memory=64),
            make_pod(uptime=600, memory=128),     # the last resort works
        ])
        self.assertTrue(self.module.launch_job(self.job))
        self.assertEqual(len(api.created), self.module.MAX_POD_ATTEMPTS + 1)
        gpus = self.gpus(api)
        self.assertEqual(gpus[-1], gpus[1], "the last pod targets the GPU of the host that reported 128 GB")
        required = int(api.created[0][2]["RAM_MIN_GB"])
        self.assertEqual(int(api.created[-1][2]["RAM_MIN_GB"]), min(required, 128))
        self.assertEqual(len(api.terminated), 4)

    def test_the_last_resort_requirement_is_lowered_to_the_ram_of_the_chosen_host(self):
        api = self.api([make_pod(uptime=30, memory=8), make_pod(uptime=30, memory=16), make_pod(uptime=30, memory=12),
                        make_pod(uptime=30, memory=16)])
        self.assertFalse(self.module.launch_job(self.job))
        required = int(api.created[0][2]["RAM_MIN_GB"])
        self.assertGreater(required, 16)
        self.assertEqual(api.created[-1][2]["RAM_MIN_GB"], "16")

    def test_when_even_the_last_resort_fails_the_job_is_given_up(self):
        api = self.api([make_pod(uptime=30, memory=32)] * 4)
        self.assertFalse(self.module.launch_job(self.job))
        self.assertEqual(len(api.created), self.module.MAX_POD_ATTEMPTS + 1)
        self.assertEqual(len(api.terminated), len(api.created))

    def test_without_any_reported_ram_there_is_no_better_host_to_pick(self):
        api = self.api([make_pod(uptime=30, memory=None)] * 5)
        self.assertFalse(self.module.launch_job(self.job))
        self.assertEqual(len(api.created), self.module.MAX_POD_ATTEMPTS)

    def test_an_output_that_is_missing_after_a_long_run_counts_as_a_failed_attempt(self):
        self.r2.file_exists_and_is_new.return_value = False
        api = self.api([make_pod(uptime=900, memory=64)] * 4)
        self.assertFalse(self.module.launch_job(self.job))
        self.assertEqual(len(api.created), self.module.MAX_POD_ATTEMPTS + 1)

    def test_pods_that_cannot_be_created_stop_after_a_few_walks_over_the_gpu_list(self):
        api = self.api([])
        api.create_error = RuntimeError("no capacity")
        self.assertFalse(self.module.launch_job(self.job))
        self.assertEqual(api.created, [])
        self.assertEqual(api.terminated, [])

    def test_a_pod_that_cannot_be_created_does_not_use_up_a_paid_attempt(self):
        api = self.api([make_pod()])
        original = api.create_pod
        failures = {"left": 2}

        def flaky(env, gpu):
            if failures["left"] > 0:
                failures["left"] -= 1
                raise RuntimeError("no capacity")
            return original(env, gpu)

        self.module.create_pod = flaky
        self.assertTrue(self.module.launch_job(self.job))
        self.assertEqual(len(api.created), 1)

    def test_the_pod_gets_its_own_credentials_when_pod_variables_are_set(self):
        api = self.api([make_pod()])
        environment = {"R2_ACCESS_KEY_ID": "pi-key", "R2_SECRET_ACCESS_KEY": "pi-secret", "R2_ENDPOINT_URL": "https://r2",
                       "POD_R2_ACCESS_KEY_ID": "pod-key", "POD_R2_SECRET_ACCESS_KEY": "pod-secret", "HF_TOKEN": "hf"}
        with patch.dict(os.environ, environment, clear=True):
            self.assertTrue(self.module.launch_job(self.job))
        env = api.created[0][2]
        self.assertEqual((env["R2_ACCESS_KEY_ID"], env["R2_SECRET_ACCESS_KEY"]), ("pod-key", "pod-secret"))
        self.assertEqual((env["R2_ENDPOINT_URL"], env["HF_TOKEN"]), ("https://r2", "hf"))
        self.assertNotIn("pi-key", str(env))
        self.assertNotIn("MESHROOM_URL", env)

    def test_the_limits_can_be_changed_with_environment_variables(self):
        with patch.dict(os.environ, {"RUNPOD_MAX_POD_ATTEMPTS": "1", "RUNPOD_MAX_CREATE_PASSES": "1"}):
            module = load_runpod(self.r2)
        self.assertEqual((module.MAX_POD_ATTEMPTS, module.MAX_CREATE_PASSES), (1, 1))


if __name__ == "__main__":
    unittest.main()
