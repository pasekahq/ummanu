"""Second source for retro: the memory-mcp search log.

The audit stream contains several memory actions. This reader accepts only records whose
`action` is `memory_search`, and pairs them with the transcript batch so the judge can tell whether a
search actually fired near an answer (the "answered from memory without memory_search" failure).

Path is overridable via UMMANU_SEARCH_LOG; by default it is the memory service's own
(`ummanu.memory.search_log.search_log_path`). The file may not exist yet, which means an empty tail.
Only the log's bounded tail is read.

Timestamps: Claude transcript ts are UTC (trailing Z); the search log writes naive UTC
(memory-mcp uses datetime.utcnow().isoformat()). Both normalize to naive UTC for comparison;
the window slack absorbs minor skew.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ummanu.memory.search_log import search_log_path, tail_lines
from ummanu.runtime.paths import env_value

DEFAULT_SEARCH_LOG = search_log_path()
SEARCH_LOG = Path(env_value("UMMANU_SEARCH_LOG", str(DEFAULT_SEARCH_LOG)))


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def tail(since=None, until=None, slack_s: int = 120) -> list[dict]:
    """search-log entries whose ts falls in [since, until] (ISO strings), widened by slack_s.

    Missing file -> []. Entries with an unparseable ts are dropped from a bounded window. Only the
    log's bounded tail is read, so a window older than it yields nothing.
    """
    lo, hi = _parse(since), _parse(until)
    if lo:
        lo -= timedelta(seconds=slack_s)
    if hi:
        hi += timedelta(seconds=slack_s)
    out = []
    for line in tail_lines(SEARCH_LOG):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("action") != "memory_search":
            continue
        t = _parse(rec.get("ts"))
        if lo and (t is None or t < lo):
            continue
        if hi and (t is None or t > hi):
            continue
        out.append(rec)
    return out


def render_markdown(entries) -> str:
    """Compact tail for the judge: one line per search with hit count."""
    if not entries:
        return "# search log: no records in the batch window.\n"
    lines = ["# Search-log tail for the batch window", ""]
    for e in entries:
        hits = e.get("hits") or []
        lines.append(f"- `{e.get('ts', '?')}` k={e.get('k', '?')} hits={len(hits)}: {e.get('query', '')}")
    return "\n".join(lines) + "\n"
