"""Capture Claude Code's statusLine rate_limits without calling a provider or reading auth.

Run as a statusLine command. --forward preserves an existing trusted shell command's stdin and
stdout. Only validated usage windows and a local observation time enter the private atomic cache.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ummanu.webproto.claude_usage import (
    CACHE_NAME,
    MAX_CACHE_BYTES,
    MAX_PAYLOAD_BYTES,
    finite_number,
    usage_limits,
)

#: How long the forwarded statusLine command may run before the render gives up on it.
FORWARD_TIMEOUT_SECONDS = 10.0


def capture(payload: bytes, home: Path, *, observed: float | None = None) -> bool:
    """Atomically replace a 0600 snapshot; an absent/malformed payload leaves the last reading intact."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        return False
    try:
        raw = json.loads(payload)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    limits = usage_limits(raw.get("rate_limits")) if isinstance(raw, dict) else {}
    if not limits:
        return False
    # A re-render is not a new provider observation. Stat only: never read transcript contents.
    if observed is None:
        transcript = raw.get("transcript_path")
        if not isinstance(transcript, str) or not Path(transcript).is_absolute():
            return False
        try:
            observed = Path(transcript).stat().st_mtime
        except OSError:
            return False
        if observed > time.time():
            return False
    if not finite_number(observed) or observed <= 0:
        return False
    directory = home / ".claude"
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Serialize heads before comparing timestamps, so an old session cannot win a write race.
        fd = os.open(directory / ".ummanu-usage.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = None
            try:
                with (directory / CACHE_NAME).open("rb") as stream:
                    previous = json.loads(stream.read(MAX_CACHE_BYTES + 1))
            except (OSError, ValueError, UnicodeDecodeError, RecursionError):
                previous = None
            if isinstance(previous, dict) and previous.get("version") == 1:
                stamp = previous.get("observed_at")
                if finite_number(stamp) and (stamp >= observed or previous.get("rate_limits") == limits):
                    return False
            fd, temporary = tempfile.mkstemp(prefix=".ummanu-usage-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "observed_at": observed, "rate_limits": limits}, stream)
            os.replace(temporary, directory / CACHE_NAME)
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass  # Best-effort capture must not prevent the original statusLine from running.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forward", help="existing trusted statusLine shell command")
    args = parser.parse_args()
    payload = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
    try:
        capture(payload, Path.home())
    except Exception:  # noqa: BLE001, S110 -- collection must never break the configured statusLine
        pass
    if args.forward and len(payload) <= MAX_PAYLOAD_BYTES:
        forward(args.forward, payload)


def forward(command: str, payload: bytes, *, timeout: float = FORWARD_TIMEOUT_SECONDS) -> None:
    """Run the configured statusLine command on `payload`; past `timeout` its whole group is killed."""
    # Exactly the configured command, never a shell fragment taken from the CLI payload.
    process = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, start_new_session=True)
    try:
        process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


if __name__ == "__main__":
    main()
