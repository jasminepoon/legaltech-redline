"""In-memory document storage, seeded from .txt files."""

import threading
from pathlib import Path


class DocumentStore:
    """Maps document ID to its current text.

    get_text and save_text do not lock. Callers that read, change, and save a
    document must hold `lock` for the whole sequence; taking it inside these
    methods as well would deadlock, because threading.Lock is not re-entrant.
    """

    def __init__(self) -> None:
        self.documents: dict[str, str] = {}
        # Version number of each document: 1 when loaded, plus 1 on every save.
        self.versions: dict[str, int] = {}
        self.lock = threading.Lock()

    def get_text(self, document_id: str) -> str | None:
        return self.documents.get(document_id)

    def get_version(self, document_id: str) -> int | None:
        return self.versions.get(document_id)

    def save_text(self, document_id: str, text: str) -> None:
        self.documents[document_id] = text
        self.versions[document_id] = self.versions.get(document_id, 0) + 1

    def snapshot(self) -> dict[str, str]:
        """Return a copy of the ID-to-text dict.

        Callers hold `lock` while taking the copy, then can read it without the
        lock. A shallow copy is enough: strings cannot change, so later edits
        replace entries in self.documents without touching the copied texts.
        """
        return dict(self.documents)

    def load_directory(self, directory: Path) -> None:
        """Replace all documents with the .txt files in directory.

        The document ID is the file name without ".txt". Files are read as
        UTF-8; text mode turns Windows line endings ("\\r\\n") into "\\n".
        A file that is not valid UTF-8 raises UnicodeDecodeError.
        """
        loaded_documents = {}
        loaded_versions = {}
        for path in sorted(directory.glob("*.txt")):
            loaded_documents[path.stem] = path.read_text(encoding="utf-8")
            loaded_versions[path.stem] = 1
        self.documents = loaded_documents
        self.versions = loaded_versions
