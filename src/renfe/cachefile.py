"""Small cache files under the cache dir: written atomically, readable only by this user."""

import contextlib
import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` with `text` in one step (mode 0600). A cache that can't be written only costs a
    refetch next time, so failures are swallowed."""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")  # mode 0600
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        tmp = None
    except (OSError, UnicodeError):
        pass
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
