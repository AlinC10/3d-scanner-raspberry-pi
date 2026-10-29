"""
When the documentation database of the AI assistant has to be rebuilt.

The Chroma database in `ai/chroma_database/` persists between runs, so the documents in `ai/ai-docs/` are parsed and
chunked only once. A fingerprint of those documents (names and contents) is saved next to the database; when the
documents change, the fingerprint no longer matches and the database is rebuilt from them.
"""
import hashlib
import os
from typing import Optional

SUPPORTED_EXTENSIONS = (".md", ".txt", ".pdf", ".docx")
FINGERPRINT_FILE = "docs_fingerprint.txt"


def docs_fingerprint(docs_path: str) -> str:
    """
    :param docs_path: folder of the documents (only its own files, like `process_directory`).
    :return: SHA-256 of the sorted names and contents of the supported files; the same for an empty or missing folder.
    """
    digest = hashlib.sha256()
    if os.path.isdir(docs_path):
        for name in sorted(os.listdir(docs_path)):
            path = os.path.join(docs_path, name)
            if not name.lower().endswith(SUPPORTED_EXTENSIONS) or not os.path.isfile(path):
                continue
            digest.update(name.encode("utf-8") + b"\0")
            with open(path, "rb") as f:
                digest.update(hashlib.sha256(f.read()).digest())
    return digest.hexdigest()


def read_fingerprint(db_path: str) -> Optional[str]:
    """:return: the fingerprint saved by `write_fingerprint`, or None when there is none."""
    try:
        with open(os.path.join(db_path, FINGERPRINT_FILE), "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_fingerprint(db_path: str, fingerprint: str) -> None:
    """Saves the fingerprint of the documents the database was built from."""
    os.makedirs(db_path, exist_ok=True)
    with open(os.path.join(db_path, FINGERPRINT_FILE), "w", encoding="utf-8") as f:
        f.write(fingerprint + "\n")


def needs_rebuild(chunk_count: int, saved: Optional[str], current: str) -> bool:
    """
    :param chunk_count: chunks now in the database.
    :param saved: fingerprint saved with the database (None for a database built before fingerprints existed).
    :param current: fingerprint of the documents now in the folder.
    :return: True when the database is empty or was built from other documents.
    """
    return chunk_count == 0 or saved != current
