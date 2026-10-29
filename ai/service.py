"""
State of the AI assistant (ScanAssist) as the server sees it: can it answer, and if not, why.

The assistant needs a Groq key (`GROQ_API_KEY`), the embedding model and the Chroma database. None of them is needed by
the scanner, so a missing piece must not stop the server: `AiService` starts with `on = False`, tries to load the
assistant (in a background thread, the embedding model takes seconds to load), and only switches to `on = True` when
everything loaded. The reason of a failure stays in `reason`, for `GET /api/ai/status` and for the 503 of
`POST /api/ai/chat`.

The `ai/` modules import each other by their bare names (`from database import db`), so the folder is put on
`sys.path` before the agent is imported.
"""
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

AI_DIR = Path(__file__).resolve().parent
KEY_ENV = "GROQ_API_KEY"

REASON_NOT_STARTED = "The AI service has not been started."
REASON_LOADING = "The AI service is starting (loading the embedding model and the documentation database)."


class AiUnavailableError(RuntimeError):
    """The assistant cannot answer: no key, a library or the database failed to load, or it is still starting."""


def _load_agent() -> Any:
    """
    Imports `ai/ai_agent.py` (Groq client, embedding model, Chroma database).

    :return: the `ai_agent` module (its `ask_agent(question, history)` answers).
    :raises Exception: whatever the import raises (missing package, key refused, database error).
    """
    if str(AI_DIR) not in sys.path:
        sys.path.append(str(AI_DIR))
    import ai_agent
    return ai_agent


class AiService:
    """
    Owns the AI assistant of the server.

    ``on`` is False until `start()` has loaded the assistant; ``loading`` is True while it loads; ``reason`` says why
    it is off (None when it is on).
    """

    def __init__(self, agent_loader: Optional[Callable[[], Any]] = None):
        """
        :param agent_loader: returns an object with ``ask_agent(question, history)``; the tests pass a fake one.
        """
        self.on = False
        self.loading = False
        self.reason: Optional[str] = REASON_NOT_STARTED
        self._agent: Any = None
        self._loader = agent_loader or _load_agent
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self, background: bool = True) -> None:
        """
        Loads the assistant once. A second call while it is loading or after it loaded does nothing; after a failure
        it tries again.

        :param background: load in a daemon thread (the server keeps starting); False loads in the caller's thread.
        """
        with self._lock:
            if self.on or self.loading:
                return
            self.loading = True
            self.reason = REASON_LOADING
        if background:
            self._thread = threading.Thread(target=self._load, name="ai-service-start", daemon=True)
            self._thread.start()
        else:
            self._load()

    def _load(self) -> None:
        agent = None
        reason = None
        try:
            try:
                from dotenv import load_dotenv
                load_dotenv()
            except ImportError:
                pass  # without python-dotenv the key can still come from the environment
            if not os.getenv(KEY_ENV, "").strip():
                raise AiUnavailableError(f"{KEY_ENV} is not set (add it to .env to enable the AI assistant).")
            agent = self._loader()
        except Exception as e:
            reason = str(e) if isinstance(e, AiUnavailableError) else f"The AI assistant could not be loaded: {e}"
            log.warning("[AI] Service off: %s", reason)
        with self._lock:
            self._agent = agent
            self.on = agent is not None
            self.reason = reason
            self.loading = False
        if agent is not None:
            log.info("[AI] Service on.")

    def wait_until_loaded(self, timeout: Optional[float] = None) -> bool:
        """Waits for a background `start()`. :return: True when no load is running any more."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.loading

    def status(self) -> dict:
        """:return: ``{"on": bool, "loading": bool, "reason": str | None}``."""
        with self._lock:
            return {"on": self.on, "loading": self.loading, "reason": self.reason}

    def ask(self, question: str, history: list) -> dict:
        """
        Answers a question with the loaded agent.

        :param question: the user's question.
        :param history: earlier messages, ``[{"role": "user" | "assistant", "text": str}, ...]``.
        :return: ``{"response": str}``.
        :raises AiUnavailableError: the service is off or still loading; the message says why.
        """
        with self._lock:
            agent = self._agent if self.on else None
            reason = self.reason
        if agent is None:
            raise AiUnavailableError(reason or REASON_NOT_STARTED)
        return agent.ask_agent(question, history)
