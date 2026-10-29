"""
The AI assistant's service state (ai/service.py) and the rebuild rule of its documentation database (ai/docs_index.py).
Both are loaded by path: they need neither FastAPI nor the AI libraries.
"""
import importlib.util
import os
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service_mod = _load("ai_service_under_test", "ai/service.py")
docs_index = _load("ai_docs_index_under_test", "ai/docs_index.py")


class FakeAgent:
    def __init__(self):
        self.calls = []

    def ask_agent(self, question, history):
        self.calls.append((question, history))
        return {"response": f"answer to {question}"}


class AiServiceTest(unittest.TestCase):
    def _service(self, loader):
        return service_mod.AiService(agent_loader=loader)

    def test_it_is_off_before_start(self):
        s = self._service(FakeAgent)
        self.assertEqual(s.status(), {"on": False, "loading": False, "reason": service_mod.REASON_NOT_STARTED})
        with self.assertRaises(service_mod.AiUnavailableError):
            s.ask("q", [])

    def test_without_the_key_it_stays_off_and_never_loads_the_agent(self):
        loader = mock.Mock(side_effect=AssertionError("the agent must not be loaded without a key"))
        s = self._service(loader)
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            s.start(background=False)
        st = s.status()
        self.assertFalse(st["on"])
        self.assertFalse(st["loading"])
        self.assertIn("GROQ_API_KEY", st["reason"])
        loader.assert_not_called()
        with self.assertRaises(service_mod.AiUnavailableError) as ctx:
            s.ask("q", [])
        self.assertIn("GROQ_API_KEY", str(ctx.exception))

    def test_a_loader_failure_leaves_it_off_with_the_reason(self):
        s = self._service(mock.Mock(side_effect=ImportError("No module named 'groq'")))
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            s.start(background=False)
        self.assertFalse(s.on)
        self.assertIn("No module named 'groq'", s.reason)

    def test_with_the_key_and_the_agent_it_is_on_and_answers(self):
        agent = FakeAgent()
        s = self._service(lambda: agent)
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            s.start(background=False)
        self.assertEqual(s.status(), {"on": True, "loading": False, "reason": None})
        self.assertEqual(s.ask("hi", [{"role": "user", "text": "x"}]), {"response": "answer to hi"})
        self.assertEqual(agent.calls, [("hi", [{"role": "user", "text": "x"}])])

    def test_background_start_reports_loading_then_on(self):
        import threading
        release = threading.Event()
        agent = FakeAgent()

        def slow_loader():
            release.wait(5)
            return agent

        s = self._service(slow_loader)
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            s.start()
            st = s.status()
            self.assertTrue(st["loading"])
            self.assertFalse(st["on"])
            self.assertEqual(st["reason"], service_mod.REASON_LOADING)
            with self.assertRaises(service_mod.AiUnavailableError):
                s.ask("too early", [])
            s.start()   # a second start while loading does nothing
            release.set()
            self.assertTrue(s.wait_until_loaded(5))
        self.assertTrue(s.on)

    def test_a_failed_start_can_be_retried(self):
        attempts = []

        def loader():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("database locked")
            return FakeAgent()

        s = self._service(loader)
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            s.start(background=False)
            self.assertFalse(s.on)
            s.start(background=False)
        self.assertTrue(s.on)
        self.assertEqual(len(attempts), 2)


class DocsIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.docs = os.path.join(self.tmp.name, "ai-docs")
        self.db = os.path.join(self.tmp.name, "db")
        os.makedirs(self.docs)
        self._write("manual.md", "first version")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, text):
        with open(os.path.join(self.docs, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_an_unchanged_folder_has_the_same_fingerprint(self):
        self.assertEqual(docs_index.docs_fingerprint(self.docs), docs_index.docs_fingerprint(self.docs))

    def test_a_changed_added_or_removed_document_changes_the_fingerprint(self):
        before = docs_index.docs_fingerprint(self.docs)
        self._write("manual.md", "second version")
        edited = docs_index.docs_fingerprint(self.docs)
        self._write("extra.txt", "new")
        added = docs_index.docs_fingerprint(self.docs)
        os.remove(os.path.join(self.docs, "extra.txt"))
        self.assertEqual(len({before, edited, added}), 3)
        self.assertEqual(docs_index.docs_fingerprint(self.docs), edited)

    def test_unsupported_files_are_ignored(self):
        before = docs_index.docs_fingerprint(self.docs)
        self._write("notes.png", "binary")
        self.assertEqual(docs_index.docs_fingerprint(self.docs), before)

    def test_the_database_is_rebuilt_only_when_empty_or_built_from_other_documents(self):
        current = docs_index.docs_fingerprint(self.docs)
        self.assertIsNone(docs_index.read_fingerprint(self.db))
        self.assertTrue(docs_index.needs_rebuild(0, None, current))          # empty
        self.assertTrue(docs_index.needs_rebuild(12, None, current))         # built before fingerprints existed
        docs_index.write_fingerprint(self.db, current)
        saved = docs_index.read_fingerprint(self.db)
        self.assertEqual(saved, current)
        self.assertFalse(docs_index.needs_rebuild(12, saved, current))       # reused as it is
        self._write("manual.md", "changed")
        self.assertTrue(docs_index.needs_rebuild(12, saved, docs_index.docs_fingerprint(self.docs)))


if __name__ == "__main__":
    unittest.main()
