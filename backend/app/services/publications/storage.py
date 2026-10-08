"""Local image storage (content-addressed). Swap for object storage later behind the same interface."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings


class ImageStorage(Protocol):
    def save(self, data: bytes, sha256: str, extension: str) -> str: ...
    def load(self, key: str) -> bytes: ...


class LocalImageStorage:
    def __init__(self, root: str | None = None):
        self.root = Path(root or get_settings().storage_path).resolve()

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid storage key")  # path traversal guard
        return p

    def save(self, data: bytes, sha256: str, extension: str) -> str:
        if not all(c in "0123456789abcdef" for c in sha256) or extension not in {"jpg", "png", "webp"}:
            raise ValueError("invalid image identity")
        key = f"items/{sha256[:2]}/{sha256}.{extension}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.chmod(tmp, 0o640)
            tmp.replace(path)
        return key

    def load(self, key: str) -> bytes:
        return self._path(key).read_bytes()
