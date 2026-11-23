from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.automations.agents.retro import search_log
from ummanu.memory import search_log as memory_search_log


class RetroSearchLogTests(unittest.TestCase):
    def test_tail_ignores_non_search_memory_audit_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "search-log.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {"ts": "2026-08-28T12:00:00+00:00", "action": "authenticate", "outcome": "allowed"},
                        {"ts": "2026-08-28T12:00:01+00:00", "action": "memory_list", "outcome": "allowed"},
                        {
                            "ts": "2026-08-28T12:00:02+00:00",
                            "action": "memory_search",
                            "outcome": "allowed",
                            "k": 5,
                            "hits": [{"id": 1, "score": 0.9}],
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(search_log, "SEARCH_LOG", path):
                entries = search_log.tail("2026-08-28T12:00:00Z", "2026-08-28T12:00:03Z", slack_s=0)

        self.assertEqual([entry["action"] for entry in entries], ["memory_search"])
        self.assertIn("hits=1", search_log.render_markdown(entries))


class MemorySearchLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "search-log.jsonl"

    def test_the_log_is_rotated_by_size_and_read_from_its_tail(self) -> None:
        lines = [json.dumps({"n": n, "pad": "x" * 40}) for n in range(20)]
        for line in lines:
            memory_search_log.append(self.path, line, max_bytes=300)

        self.assertLessEqual(self.path.stat().st_size, 300)
        self.assertLessEqual(memory_search_log.rotated(self.path).stat().st_size, 300)
        tail = memory_search_log.tail_lines(self.path, max_bytes=200)
        self.assertLessEqual(sum(len(line) + 1 for line in tail), 200)
        self.assertEqual(tail, lines[-len(tail) :])
        self.assertTrue(tail)
        everything = memory_search_log.tail_lines(self.path, max_bytes=10_000)
        self.assertEqual(everything[-1], lines[-1])
        self.assertGreater(len(everything), len(tail))

    def test_a_missing_log_has_no_lines(self) -> None:
        self.assertEqual(memory_search_log.tail_lines(self.path), [])

    def test_the_service_and_retro_resolve_one_path_from_the_memory_db(self) -> None:
        env = {"MEMORY_DB": "/data/memory/index.sqlite"}
        self.assertEqual(memory_search_log.search_log_path(env), Path("/data/memory/search-log.jsonl"))
        env["MEMORY_SEARCH_LOG"] = "/elsewhere/log.jsonl"
        self.assertEqual(memory_search_log.search_log_path(env), Path("/elsewhere/log.jsonl"))
        with mock.patch.dict(os.environ, {"MEMORY_DB": "/data/memory/index.sqlite"}):
            importlib.reload(search_log)
            self.addCleanup(importlib.reload, search_log)
            self.assertEqual(search_log.SEARCH_LOG, Path("/data/memory/search-log.jsonl"))


if __name__ == "__main__":
    unittest.main()
