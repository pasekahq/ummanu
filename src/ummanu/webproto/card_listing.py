"""One Pipeline listing per request for a page with two card readers; outside a pin each reads alone."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from ummanu.tasks import TaskReader

if TYPE_CHECKING:
    from ummanu.sprints import SprintReader

_PIN: ContextVar[dict[str, list[dict[str, Any]]] | None] = ContextVar("pipeline-listing", default=None)


@contextmanager
def one_listing() -> Iterator[None]:
    """Share the first Pipeline listing of this context with every later read in it."""
    token = _PIN.set({})
    try:
        yield
    finally:
        _PIN.reset(token)


def linked_cards(reader: SprintReader) -> dict[str, list[dict[str, Any]]]:
    """`reader.linked_cards(include_archived=True)`, or the same grouping of the pinned listing."""
    pin = _PIN.get()
    if pin is None:
        return reader.linked_cards(include_archived=True)
    linked: dict[str, list[dict[str, Any]]] = {}
    for card in _pinned(pin, reader.client):
        linked.setdefault(str(card.get("sprint") or ""), []).append(card)
    return linked


def current_cards(client: Any, states: Collection[str]) -> list[dict[str, Any]]:
    """The open cards in `states`: `TaskReader.list(states=...)`, or the same rows of the pinned listing."""
    pin = _PIN.get()
    if pin is None:
        return TaskReader(client).list(states=set(states))
    return [card for card in _pinned(pin, client) if not card.get("closed") and card.get("state") in states]


def _pinned(pin: dict[str, list[dict[str, Any]]], client: Any) -> list[dict[str, Any]]:
    """The pinned listing, read on first use; a refusal is not pinned."""
    if "cards" not in pin:
        pin["cards"] = TaskReader(client).list(include_archived=True)
    return pin["cards"]
