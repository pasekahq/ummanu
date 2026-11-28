"""The installation's one slot for a heavy local check: a local gate run or a full broad check.

The slot is an exclusive `flock` on `<data_dir>/dispatcher/heavy-check.lock`. The kernel drops it
with the process that holds it, so a crashed holder frees the slot without any cleanup.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

SLOT_FILE = Path("dispatcher") / "heavy-check.lock"
# Names the slot a process already runs inside, so a nested check (a gate command that runs
# `ummanu check broad`) proceeds instead of waiting on its own parent.
HELD_ENV = "UMMANU_HEAVY_CHECK_SLOT"


def slot_path(data_dir: Path) -> Path:
    return Path(data_dir) / SLOT_FILE


@dataclass(frozen=True)
class Slot:
    """Whether this process holds the slot; `holder` names the other holder when it does not."""

    held: bool
    holder: str = ""
    # Environment a child needs to run inside this slot.
    env: dict[str, str] = field(default_factory=dict)


@contextlib.contextmanager
def host_slot(path: Path, label: str, *, on_busy: Callable[[str], None] | None = None) -> Iterator[Slot]:
    """Hold the slot for the block.

    When another process holds it, yield a busy Slot, or with `on_busy` report the holder to it and
    wait for the slot.
    """
    if os.environ.get(HELD_ENV) == str(path):
        yield Slot(True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip() or "an unnamed process"
            if on_busy is None:
                yield Slot(False, holder)
                return
            on_busy(holder)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.truncate(0)
            handle.write(f"pid {os.getpid()}: {label}\n")
            handle.flush()
            yield Slot(True, env={HELD_ENV: str(path)})
        finally:
            handle.truncate(0)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
