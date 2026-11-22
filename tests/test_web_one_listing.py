"""One Pipeline listing per page request: the dashboard and a project page share it.

`TaskReader.list` is counted at the class, so every reader of every layer is seen. The board client
answers nothing else, so each other source of the page is unavailable and the count is the listing
alone. Nothing here reaches a live installation or a board store.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from tests.web_fakes import Recording
from ummanu.tasks import TaskReader
from ummanu.web.app import WebApp
from ummanu.webproto.card_listing import one_listing
from ummanu.webproto.reads import ReadLayer
from ummanu.webproto.sprint_reads import SprintReadLayer

NOW = 1788652800.0


class Unreadable:
    """A board client every other read of the page is refused by."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("no board store in this test")

        return refuse


def card(ref: str, state: str, *, closed: bool = False, sprint: str | None = None) -> dict[str, Any]:
    return {
        "ref": ref,
        "id": ref,
        "title": ref,
        "state": state,
        "position": 1,
        "project": "ummanu",
        "sprint": sprint,
        "closed": closed,
    }


CARDS = [
    card("ummanu-1", "in_progress", sprint="sprint:7"),
    card("ummanu-2", "done", closed=True, sprint="sprint:7"),
    card("ummanu-3", "ready"),
]


class OneListingTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data_dir = root / "data"
        data_dir.mkdir()
        self.instance = root / "instance"
        (self.instance / "projects").mkdir(parents=True)
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: test\n"
            f"data_dir: {data_dir}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        repo = root / "repos" / "ummanu"
        repo.mkdir(parents=True)
        binding = {
            "id": "ummanu",
            "repo": str(repo),
            "enabled": True,
            "adapter": "ummanu",
            "default_branch": "main",
        }
        (self.instance / "projects" / "ummanu.yaml").write_text(yaml.safe_dump(binding), encoding="utf-8")
        self.listings: list[dict[str, Any]] = []

        def listing(_reader: TaskReader, **arguments: Any) -> list[dict[str, Any]]:
            self.listings.append(arguments)
            if arguments.get("include_archived"):
                return [dict(row) for row in CARDS]
            states = arguments.get("states")
            return [
                dict(row) for row in CARDS if not row["closed"] and (not states or row["state"] in states)
            ]

        self.enterContext(mock.patch.object(TaskReader, "list", autospec=True, side_effect=listing))
        self.reads = ReadLayer(self.instance, board_client=Unreadable(), clock=lambda: NOW, data_dir=data_dir)
        sprint_reads = SprintReadLayer(
            self.instance, board_client=Unreadable(), clock=lambda: NOW, data_dir=data_dir
        )
        self.app = WebApp(self.reads, Recording(), sprint_reads, *(Recording() for _ in range(5)))

    def get(self, path: str) -> int:
        self.listings.clear()
        response = self.app.handle("GET", path)
        self.assertEqual(response.status, 200, response.body[:400])
        return len(self.listings)

    def test_the_dashboard_and_a_project_page_list_the_pipeline_once(self) -> None:
        self.assertEqual(self.get("/"), 1, self.listings)
        self.assertEqual(self.get("/projects/ummanu"), 1, self.listings)

    def test_pages_with_one_reader_keep_their_own_listing(self) -> None:
        self.assertEqual(self.get("/sprints"), 1, self.listings)
        self.assertEqual(self.get("/projects"), 1, self.listings)
        self.assertEqual(self.listings, [{"states": mock.ANY}])

    def test_a_pinned_snapshot_carries_only_the_open_current_cards(self) -> None:
        unpinned = self.reads.system_snapshot()["tasks"]["items"]
        with one_listing():
            pinned = self.reads.system_snapshot()["tasks"]["items"]
        self.assertEqual(pinned, unpinned)
        self.assertEqual([item["ref"] for item in pinned], ["ummanu-1", "ummanu-3"])

    def test_each_request_lists_afresh(self) -> None:
        self.get("/")
        self.assertEqual(self.get("/"), 1, self.listings)
