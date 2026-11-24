"""The memory service's search log: its one path, its size-bounded append and its bounded tail.

The service appends to it and retro reads it, so both resolve the path here: `MEMORY_SEARCH_LOG`, else
`search-log.jsonl` beside the index `MEMORY_DB` names, else under `~/ummanu-data/memory`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_MEMORY_DIR = Path.home() / "ummanu-data" / "memory"
LOG_NAME = "search-log.jsonl"
#: Past this size the log is moved aside to `<name>.1` (replacing the previous one) before an append.
MAX_LOG_BYTES = 8 * 1024 * 1024
#: How much of the log a reader takes from its end, across the live file and the rotated one.
TAIL_BYTES = 4 * 1024 * 1024


def memory_db_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("MEMORY_DB") or DEFAULT_MEMORY_DIR / "index.sqlite")


def search_log_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("MEMORY_SEARCH_LOG")
    return Path(configured) if configured else memory_db_path(env).parent / LOG_NAME


def rotated(path: Path) -> Path:
    return path.with_name(path.name + ".1")


def append(path: Path, line: str, *, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Append one line, first moving a log that would grow past `max_bytes` aside. Callers serialize."""
    data = (line + "\n").encode("utf-8")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if size and size + len(data) > max_bytes:
        os.replace(path, rotated(path))
    with open(path, "ab") as stream:
        stream.write(data)


def tail_lines(path: Path, *, max_bytes: int = TAIL_BYTES) -> list[str]:
    """The complete lines in the last `max_bytes` of the log, oldest first; a missing log has none."""
    current = _tail(path, max_bytes)
    older = _tail(rotated(path), max_bytes - len(current)) if len(current) < max_bytes else b""
    text = (older + current).decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()]


def _tail(path: Path, max_bytes: int) -> bytes:
    """The last `max_bytes` of `path` from the first line that starts inside them."""
    if max_bytes <= 0:
        return b""
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            # One byte before the window tells whether its first line starts at the window's edge.
            start = max(0, size - max_bytes - 1)
            stream.seek(start)
            data = stream.read(size - start)
    except FileNotFoundError:
        return b""
    if start:
        cut = data.find(b"\n")
        data = b"" if cut < 0 else data[cut + 1 :]
    if data and not data.endswith(b"\n"):
        data += b"\n"
    return data
