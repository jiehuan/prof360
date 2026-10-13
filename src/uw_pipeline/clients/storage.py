"""Raw landing-zone storage. LocalStorage now; an ADLS implementation can replace it later."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    def exists(self, key: str) -> bool: ...
    def read_bytes(self, key: str) -> bytes: ...
    def write_bytes(self, key: str, data: bytes) -> str:
        """Write `data` under `key` and return a locator (path/URI) recorded for lineage."""
        ...


class LocalStorage:
    """Stores objects as files under `root`; keys are '/'-separated relative paths."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"storage key escapes root: {key!r}")
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def write_bytes(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a killed run never leaves a truncated raw file behind.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return str(path)
