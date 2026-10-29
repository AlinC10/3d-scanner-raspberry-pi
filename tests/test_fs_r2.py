"""
Tests for cloud/cloudflare_r2.py (FIRST_SCAN_PLAN P1/P7, audit CLOUD-16).

The module is loaded by file path with fake boto3 / botocore / dotenv modules and an in-memory FakeS3 client, so
nothing touches the network and nothing depends on (or pollutes) the MagicMocks installed by tests/__init__.py.
"""
import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

R2_ENV = {
    "R2_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
    "R2_ACCESS_KEY_ID": "key",
    "R2_SECRET_ACCESS_KEY": "secret",
}


class FakeClientError(Exception):
    def __init__(self, error_response=None, operation_name="op"):
        super().__init__(f"ClientError {operation_name}: {error_response}")
        self.response = error_response or {"Error": {"Code": "500", "Message": "boom"}}
        self.operation_name = operation_name


class FakeBotoCoreError(Exception):
    pass


class FakeS3UploadFailedError(Exception):
    pass


class FakePaginator:
    def __init__(self, client, page_size):
        self.client = client
        self.page_size = page_size

    def paginate(self, Bucket, Prefix=""):
        self.client.calls.append(("paginate", Bucket, Prefix))
        if self.client.fail_on == "list":
            raise FakeClientError(operation_name="ListObjectsV2")
        keys = sorted(k for k in self.client.objects if k.startswith(Prefix))
        if not keys:
            yield {"KeyCount": 0}
            return
        for i in range(0, len(keys), self.page_size):
            yield {"Contents": [{"Key": k} for k in keys[i:i + self.page_size]]}


class FakeS3:
    """In-memory bucket. fail_on: None | 'list' | 'put' | 'head' | 'upload' | 'delete_objects' | 'delete_object'."""

    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_on = None
        self.upload_exc = FakeClientError
        self.page_size = 1000
        self.delete_errors = set()  # keys reported in the 'Errors' list of delete_objects
        self.chunks = [1]           # bytes reported to the progress callback, one call per chunk
        self.chunk_pause = 0.0      # seconds each chunk takes (a slow uplink)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self, self.page_size)

    def delete_objects(self, Bucket, Delete):
        self.calls.append(("delete_objects", Bucket, len(Delete["Objects"])))
        if self.fail_on == "delete_objects":
            raise FakeClientError(operation_name="DeleteObjects")
        errors = []
        for entry in Delete["Objects"]:
            key = entry["Key"]
            if key in self.delete_errors:
                errors.append({"Key": key, "Code": "AccessDenied", "Message": "denied"})
            else:
                self.objects.pop(key, None)
        response = {}
        if errors:
            response["Errors"] = errors
        return response

    def delete_object(self, Bucket, Key):
        self.calls.append(("delete_object", Bucket, Key))
        if self.fail_on == "delete_object":
            raise FakeClientError(operation_name="DeleteObject")
        self.objects.pop(Key, None)

    def put_object(self, Bucket, Key, Body):
        self.calls.append(("put_object", Bucket, Key))
        if self.fail_on == "put":
            raise FakeClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        self.objects[Key] = Body

    def head_object(self, Bucket, Key):
        self.calls.append(("head_object", Bucket, Key))
        if self.fail_on == "head":
            raise FakeClientError({"Error": {"Code": "404"}}, "HeadObject")
        if Key not in self.objects:
            raise FakeClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def upload_file(self, filename, bucket, key, Callback=None):
        self.calls.append(("upload_file", filename, bucket, key))
        if self.fail_on == "upload":
            raise self.upload_exc("upload failed")
        # like s3transfer: the progress callback runs as the bytes go out, and what it raises aborts the transfer
        for chunk in self.chunks:
            if self.chunk_pause:
                time.sleep(self.chunk_pause)
            if Callback is not None:
                Callback(chunk)
        self.objects[key] = b"x"


def _fake_modules(client, config_supports):
    """config_supports: set of keyword names the fake botocore Config accepts."""
    configs = []

    class Config:
        def __init__(self, **kwargs):
            unknown = set(kwargs) - config_supports
            if unknown:
                raise TypeError(f"Config() got unexpected keyword arguments {sorted(unknown)}")
            self.kwargs = kwargs
            configs.append(self)

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *args, **kwargs: client
    boto3_exceptions = types.ModuleType("boto3.exceptions")
    boto3_exceptions.S3UploadFailedError = FakeS3UploadFailedError
    boto3.exceptions = boto3_exceptions

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = Config
    botocore_exceptions = types.ModuleType("botocore.exceptions")
    botocore_exceptions.ClientError = FakeClientError
    botocore_exceptions.BotoCoreError = FakeBotoCoreError
    botocore.config = botocore_config
    botocore.exceptions = botocore_exceptions

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: False

    return {
        "boto3": boto3, "boto3.exceptions": boto3_exceptions,
        "botocore": botocore, "botocore.config": botocore_config, "botocore.exceptions": botocore_exceptions,
        "dotenv": dotenv,
    }


ALL_CONFIG_KEYS = {
    "connect_timeout", "read_timeout", "retries", "region_name",
    "request_checksum_calculation", "response_checksum_validation",
}
OLD_CONFIG_KEYS = {"connect_timeout", "read_timeout", "retries", "region_name"}


def _load_r2(client=None, config_supports=ALL_CONFIG_KEYS):
    client = client if client is not None else FakeS3()
    modules = _fake_modules(client, set(config_supports))
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "cloudflare_r2_under_test", os.path.join(ROOT, "cloud", "cloudflare_r2.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, client


class ConfigTests(unittest.TestCase):
    def test_config_has_contract_options(self):
        r2, _ = _load_r2()
        self.assertEqual(r2.R2_CONFIG.kwargs, {
            "connect_timeout": 10,
            "read_timeout": 60,
            "retries": {"max_attempts": 1},
            "region_name": "auto",
            "request_checksum_calculation": "when_required",
            "response_checksum_validation": "when_required",
        })

    def test_config_falls_back_when_checksum_keywords_unsupported(self):
        r2, _ = _load_r2(config_supports=OLD_CONFIG_KEYS)
        self.assertEqual(r2.R2_CONFIG.kwargs, {
            "connect_timeout": 10, "read_timeout": 60, "retries": {"max_attempts": 1}, "region_name": "auto",
        })

    def test_config_falls_back_to_timeouts_only(self):
        r2, _ = _load_r2(config_supports={"connect_timeout", "read_timeout", "retries"})
        self.assertEqual(r2.R2_CONFIG.kwargs, {
            "connect_timeout": 10, "read_timeout": 60, "retries": {"max_attempts": 1},
        })


class ListAndVerifyTests(unittest.TestCase):
    def setUp(self):
        self.r2, self.s3 = _load_r2()

    def test_list_keys_is_paginated(self):
        self.s3.page_size = 1000
        self.s3.objects = {f"rig/0/{i:05d}.jpg": b"x" for i in range(2500)}
        keys = self.r2.list_keys()
        self.assertEqual(len(keys), 2500)
        self.assertIsInstance(keys, set)
        self.assertEqual(sum(1 for c in self.s3.calls if c[0] == "paginate"), 1)

    def test_list_keys_prefix(self):
        self.s3.objects = {"rig/0/a.jpg": b"x", "rig/1/b.jpg": b"x", "other.txt": b"x"}
        self.assertEqual(self.r2.list_keys("rig/"), {"rig/0/a.jpg", "rig/1/b.jpg"})

    def test_list_keys_empty_bucket(self):
        self.assertEqual(self.r2.list_keys(), set())

    def test_list_keys_failure_raises_r2error(self):
        self.s3.fail_on = "list"
        with self.assertRaises(self.r2.R2Error):
            self.r2.list_keys()

    def test_verify_keys_returns_missing_sorted(self):
        self.s3.page_size = 2
        self.s3.objects = {"b": b"x", "d": b"x", "a": b"x"}
        missing = self.r2.verify_keys(["d", "z", "a", "c", "b", "y"])
        self.assertEqual(missing, ["c", "y", "z"])

    def test_verify_keys_all_present(self):
        self.s3.objects = {"a": b"x", "b": b"x"}
        self.assertEqual(self.r2.verify_keys({"a", "b"}), [])

    def test_verify_keys_uses_one_listing_not_one_request_per_key(self):
        self.s3.objects = {f"k{i}": b"x" for i in range(50)}
        self.r2.verify_keys([f"k{i}" for i in range(100)])
        self.assertEqual([c[0] for c in self.s3.calls], ["paginate"])

    def test_verify_keys_listing_failure_raises(self):
        self.s3.fail_on = "list"
        with self.assertRaises(self.r2.R2Error):
            self.r2.verify_keys(["a"])


class DeleteAllTests(unittest.TestCase):
    def setUp(self):
        self.r2, self.s3 = _load_r2()

    def test_2500_keys_deleted_in_chunks_of_1000(self):
        self.s3.objects = {f"rig/0/{i:05d}.jpg": b"x" for i in range(2500)}
        deleted = self.r2.delete_all_files_from_bucket()
        self.assertEqual(deleted, 2500)
        self.assertEqual(self.s3.objects, {})
        sizes = [c[2] for c in self.s3.calls if c[0] == "delete_objects"]
        self.assertEqual(sizes, [1000, 1000, 500])

    def test_empty_bucket_returns_zero_and_deletes_nothing(self):
        self.assertEqual(self.r2.delete_all_files_from_bucket(), 0)
        self.assertFalse([c for c in self.s3.calls if c[0] == "delete_objects"])

    def test_errors_in_response_raise_r2error_listing_keys(self):
        self.s3.objects = {f"k{i}": b"x" for i in range(5)}
        self.s3.delete_errors = {"k1", "k3"}
        with self.assertRaises(self.r2.R2Error) as ctx:
            self.r2.delete_all_files_from_bucket()
        message = str(ctx.exception)
        self.assertIn("k1", message)
        self.assertIn("k3", message)
        self.assertNotIn("k2", message)

    def test_errors_in_a_later_chunk_are_still_reported(self):
        self.s3.objects = {f"k{i:04d}": b"x" for i in range(1500)}
        self.s3.delete_errors = {"k1400"}
        with self.assertRaises(self.r2.R2Error) as ctx:
            self.r2.delete_all_files_from_bucket()
        self.assertIn("k1400", str(ctx.exception))

    def test_delete_request_failure_raises_r2error(self):
        self.s3.objects = {"a": b"x"}
        self.s3.fail_on = "delete_objects"
        with self.assertRaises(self.r2.R2Error):
            self.r2.delete_all_files_from_bucket()


class UploadFileTests(unittest.TestCase):
    def setUp(self):
        self.r2, self.s3 = _load_r2()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "0001.jpg")
        with open(self.path, "wb") as f:
            f.write(b"jpeg-bytes")

    def test_success(self):
        self.assertTrue(self.r2.upload_file(self.path, object_name="rig/0/0001.jpg"))
        self.assertIn("rig/0/0001.jpg", self.s3.objects)

    def test_default_object_name_is_basename(self):
        self.assertTrue(self.r2.upload_file(self.path))
        self.assertIn("0001.jpg", self.s3.objects)

    def test_returns_false_on_each_exception_type_and_never_raises(self):
        for exc in (FakeClientError, FakeBotoCoreError, FakeS3UploadFailedError, OSError):
            with self.subTest(exc=exc.__name__):
                self.s3.fail_on = "upload"
                self.s3.upload_exc = exc
                self.assertFalse(self.r2.upload_file(self.path))

    def test_missing_file_returns_false_without_network(self):
        missing = os.path.join(self.tmp.name, "nope.jpg")
        self.assertFalse(self.r2.upload_file(missing))
        self.assertEqual(self.s3.calls, [])

    def test_zero_byte_file_returns_false_without_network(self):
        empty = os.path.join(self.tmp.name, "empty.jpg")
        open(empty, "wb").close()
        self.assertFalse(self.r2.upload_file(empty))
        self.assertEqual(self.s3.calls, [])

    def test_directory_is_not_uploaded(self):
        self.assertFalse(self.r2.upload_file(self.tmp.name))
        self.assertEqual(self.s3.calls, [])

    def test_an_attempt_longer_than_its_limit_is_abandoned(self):
        # E2: a transfer that keeps trickling bytes is cut at the attempt limit and reported as a failure
        self.s3.chunks = [1] * 50
        self.s3.chunk_pause = 0.02     # the whole transfer would take 1 s
        started = time.monotonic()
        self.assertFalse(self.r2.upload_file(self.path, object_name="rig/0/slow.jpg", timeout_s=0.1))
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertNotIn("rig/0/slow.jpg", self.s3.objects)

    def test_an_attempt_within_its_limit_succeeds_and_zero_disables_the_limit(self):
        self.s3.chunks = [1] * 5
        self.s3.chunk_pause = 0.01
        self.assertTrue(self.r2.upload_file(self.path, object_name="a.jpg", timeout_s=5.0))
        self.assertTrue(self.r2.upload_file(self.path, object_name="b.jpg", timeout_s=0))

    def test_the_default_attempt_limit_is_five_minutes(self):
        if "SCAN_UPLOAD_ATTEMPT_TIMEOUT_S" in os.environ:
            self.skipTest("SCAN_UPLOAD_ATTEMPT_TIMEOUT_S override present in the environment")
        self.assertEqual(self.r2.UPLOAD_ATTEMPT_TIMEOUT_S, 300.0)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.r2, self.s3 = _load_r2()

    def _env(self, **overrides):
        env = dict(R2_ENV)
        env.update(overrides)
        return patch.dict(os.environ, env, clear=True)

    def test_missing_env_raises_and_names_variable(self):
        for name in R2_ENV:
            with self.subTest(missing=name):
                self.s3.calls.clear()
                with self._env(**{name: ""}):
                    with self.assertRaises(self.r2.R2Error) as ctx:
                        self.r2.preflight()
                self.assertIn(name, str(ctx.exception))
                self.assertEqual(self.s3.calls, [])

    def test_env_entirely_absent_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(self.r2.R2Error):
                self.r2.preflight()

    def test_write_failure_raises_r2error(self):
        self.s3.fail_on = "put"
        with self._env():
            with self.assertRaises(self.r2.R2Error) as ctx:
                self.r2.preflight()
        self.assertIn("write test failed", str(ctx.exception))
        self.assertEqual(self.s3.objects, {})

    def test_success_leaves_no_object(self):
        with self._env():
            self.assertIsNone(self.r2.preflight())
        self.assertEqual(self.s3.objects, {})
        ops = [c[0] for c in self.s3.calls]
        self.assertEqual(ops, ["put_object", "head_object", "delete_object"])
        self.assertTrue(self.s3.calls[0][2].startswith("_preflight/"))

    def test_head_failure_still_deletes_probe_and_raises(self):
        self.s3.fail_on = "head"
        with self._env():
            with self.assertRaises(self.r2.R2Error):
                self.r2.preflight()
        self.assertEqual(self.s3.objects, {})

    def test_delete_failure_raises(self):
        self.s3.fail_on = "delete_object"
        with self._env():
            with self.assertRaises(self.r2.R2Error) as ctx:
                self.r2.preflight()
        self.assertIn("delete test failed", str(ctx.exception))


class OtherHelpersTests(unittest.TestCase):
    def test_delete_file_returns_false_on_botocore_error(self):
        r2, s3 = _load_r2()
        s3.delete_object = lambda **kwargs: (_ for _ in ()).throw(FakeBotoCoreError("x"))
        self.assertFalse(r2.delete_file("a"))

    def test_file_exists_and_is_new_false_on_botocore_error(self):
        r2, s3 = _load_r2()
        s3.head_object = lambda **kwargs: (_ for _ in ()).throw(FakeBotoCoreError("x"))
        self.assertFalse(r2.file_exists_and_is_new("a"))


if __name__ == "__main__":
    unittest.main()
