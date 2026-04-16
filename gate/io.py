"""Atomic filesystem write helpers.

Writes go through a sibling `.tmp` file and then `os.replace` to the final
path. On any failure the tmp file is cleaned up so callers never see a
partially written target file and never leak tmp siblings.

Portability: `os.replace` is atomic on POSIX and Windows for same-filesystem
renames, so these helpers work everywhere the package runs. Use them for any
full-file rewrite (counters, JSON blobs, JSONL trims).
"""

import os
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    """Write text content atomically via tmp sibling + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write bytes atomically via tmp sibling + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
