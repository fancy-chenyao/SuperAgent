"""Small dependency-free cross-process file lock."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional


class FileLock:
    """Coarse cross-process mutex based on an exclusive ``.lock`` file."""

    def __init__(self, path: Path, *, timeout: float = 10.0, poll: float = 0.05) -> None:
        self._path = Path(str(path) + ".lock")
        self._timeout = timeout
        self._poll = poll
        self._fd: Optional[int] = None

    def __enter__(self) -> "FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        while True:
            try:
                self._fd = os.open(
                    str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                return self
            except FileExistsError:
                waited = time.time() - start
                try:
                    if time.time() - self._path.stat().st_mtime > self._timeout:
                        os.unlink(self._path)
                        continue
                except OSError:
                    pass
                if waited > self._timeout:
                    raise TimeoutError(f"could not acquire lock {self._path}")
                time.sleep(self._poll)

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
        except OSError:
            pass
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass
