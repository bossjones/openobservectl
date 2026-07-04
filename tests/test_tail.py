"""Tests for openobservectl.tail — window advance, boundary dedup, and the follow loop (TDD).

OpenObserve has no push/subscribe API for new logs, so "tail -f" is sliding-window
polling on the microsecond ``_timestamp`` column. These tests exercise the pure
windowing/dedup logic first, then the async follow/fan-out engine built on it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pytest_httpserver import HTTPServer

from openobservectl.tail import (
    AsyncSearchClient,
    build_tail_sql,
    follow,
    now_micros,
    run_tail,
    select_new_hits,
)


def _hit(ts: int, msg: str) -> dict:
    return {"_timestamp": ts, "log": msg}


# -- select_new_hits (pure window/dedup logic) -----------------------------


def test_select_all_new_on_first_window() -> None:
    hits = [_hit(3, "c"), _hit(1, "a"), _hit(2, "b")]
    emitted, last_ts, seen = select_new_hits(hits, last_ts=0, seen=set())
    assert [h["log"] for h in emitted] == ["a", "b", "c"]  # sorted by ts
    assert last_ts == 3
    assert len(seen) == 1  # only the record(s) at the max ts are tracked


def test_empty_poll_leaves_state_unchanged() -> None:
    emitted, last_ts, seen = select_new_hits([], last_ts=5, seen={"x"})
    assert emitted == []
    assert last_ts == 5
    assert seen == {"x"}


def test_boundary_record_not_re_emitted_across_polls() -> None:
    # poll 1
    hits1 = [_hit(1, "a"), _hit(2, "b")]
    emitted1, last_ts, seen = select_new_hits(hits1, last_ts=0, seen=set())
    assert [h["log"] for h in emitted1] == ["a", "b"]
    assert last_ts == 2
    # poll 2 re-returns the boundary record b (ts==2) plus a new c (ts==3)
    hits2 = [_hit(2, "b"), _hit(3, "c")]
    emitted2, last_ts, seen = select_new_hits(hits2, last_ts=last_ts, seen=seen)
    assert [h["log"] for h in emitted2] == ["c"]  # b deduped
    assert last_ts == 3


def test_two_records_sharing_max_ts_both_emitted_then_deduped() -> None:
    hits1 = [_hit(5, "a")]
    emitted1, last_ts, seen = select_new_hits(hits1, last_ts=0, seen=set())
    assert [h["log"] for h in emitted1] == ["a"]
    # a second record with the SAME ts arrives next poll -> emit it once
    hits2 = [_hit(5, "a"), _hit(5, "b")]
    emitted2, last_ts, seen = select_new_hits(hits2, last_ts=last_ts, seen=seen)
    assert [h["log"] for h in emitted2] == ["b"]
    assert last_ts == 5
    # third poll re-returns both -> nothing new
    emitted3, last_ts, seen = select_new_hits(hits2, last_ts=last_ts, seen=seen)
    assert emitted3 == []


# -- build_tail_sql --------------------------------------------------------


def test_build_tail_sql_default_orders_by_timestamp() -> None:
    sql = build_tail_sql(stream="syslog", sql=None)
    assert 'FROM "syslog"' in sql
    assert "_timestamp" in sql.lower()


def test_build_tail_sql_uses_explicit_sql() -> None:
    assert build_tail_sql(stream="x", sql="SELECT foo FROM bar") == "SELECT foo FROM bar"


# -- now_micros -------------------------------------------------------------


def test_now_micros_returns_a_plausible_epoch_microsecond_value() -> None:
    # 1_700_000_000_000_000 us == 2023-11-14 — sanity bound, catches ms/s unit mistakes
    assert now_micros() > 1_700_000_000_000_000


# -- follow loop (stub client) ---------------------------------------------


class StubClient:
    """Returns scripted pages on successive search() calls; records windows."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages: list[list[dict[str, Any]]] = list(pages)
        self.calls: list[tuple[int, int]] = []

    async def search(
        self, *, sql: str, start_time: int, end_time: int, size: int, from_: int = 0
    ) -> list[dict[str, Any]]:
        self.calls.append((start_time, end_time))
        return self.pages.pop(0) if self.pages else []


async def _drain(queue: asyncio.Queue) -> list[dict]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def test_follow_single_poll_emits_hits() -> None:
    client = StubClient([[_hit(1, "a"), _hit(2, "b")]])
    q: asyncio.Queue = asyncio.Queue()
    await follow(
        client, stream="s", sql=None, since_micros=0, interval=0, size=100, queue=q, follow=False
    )
    assert [h["log"] for h in await _drain(q)] == ["a", "b"]


async def test_follow_advances_window_and_dedups_across_polls() -> None:
    client = StubClient(
        [
            [_hit(1, "a"), _hit(2, "b")],
            [_hit(2, "b"), _hit(3, "c")],  # b is a boundary dup
        ]
    )
    q: asyncio.Queue = asyncio.Queue()
    slept: list[float] = []

    async def _sleep(s: float) -> None:
        slept.append(s)

    await follow(
        client,
        stream="s",
        sql=None,
        since_micros=0,
        interval=0.01,
        size=100,
        queue=q,
        follow=True,
        max_polls=2,
        sleep_fn=_sleep,
    )
    assert [h["log"] for h in await _drain(q)] == ["a", "b", "c"]
    # second poll's window starts at the max ts seen in poll 1 (inclusive, for dedup)
    assert client.calls[1][0] == 2
    assert slept  # follow mode sleeps between polls


async def test_follow_respects_stop_event() -> None:
    client = StubClient([[_hit(1, "a")], [_hit(2, "b")], [_hit(3, "c")]])
    q: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    stop.set()  # already stopped before the loop starts
    slept: list[float] = []

    async def _sleep(s: float) -> None:
        slept.append(s)

    await follow(
        client,
        stream="s",
        sql=None,
        since_micros=0,
        interval=0.01,
        size=100,
        queue=q,
        follow=True,
        stop=stop,
        sleep_fn=_sleep,
    )
    assert await _drain(q) == []
    assert client.calls == []
    assert slept == []


# -- run_tail (fan-out across streams) --------------------------------------


async def test_run_tail_fans_out_streams_to_a_shared_consumer() -> None:
    client = StubClient([[_hit(1, "a")], [_hit(9, "z")]])
    got: list[dict] = []
    await run_tail(
        client,
        streams=["s1", "s2"],
        sql=None,
        since_micros=0,
        interval=0,
        size=100,
        follow=False,
        on_hit=got.append,
    )
    assert {h["log"] for h in got} == {"a", "z"}


async def test_run_tail_propagates_a_producer_exception() -> None:
    class FailingClient:
        async def search(
            self, *, sql: str, start_time: int, end_time: int, size: int, from_: int = 0
        ) -> list[dict[str, Any]]:
            if "bad" in sql:
                raise RuntimeError("search failed")
            return []

    got: list[dict] = []
    try:
        await run_tail(
            FailingClient(),
            streams=["ok", "bad"],
            sql=None,
            since_micros=0,
            interval=0,
            size=100,
            follow=False,
            on_hit=got.append,
        )
    except RuntimeError as exc:
        assert "search failed" in str(exc)
    else:
        raise AssertionError("expected RuntimeError to propagate")


# -- AsyncSearchClient (httpx.AsyncClient adapter to the _Searcher protocol) --


async def test_async_search_client_posts_expected_body_and_parses_hits(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request(
        "/api/default/_search",
        method="POST",
        json={"query": {"sql": "SELECT * FROM x", "start_time": 1, "end_time": 2, "size": 100}},
    ).respond_with_json({"hits": [{"_timestamp": 1, "log": "hi"}], "total": 1})
    async with httpx.AsyncClient(base_url=httpserver.url_for("")) as client:
        searcher = AsyncSearchClient(client, org="default")
        hits = await searcher.search(sql="SELECT * FROM x", start_time=1, end_time=2, size=100)
    assert hits == [{"_timestamp": 1, "log": "hi"}]


async def test_async_search_client_returns_empty_list_when_hits_missing(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/api/default/_search", method="POST").respond_with_json({})
    async with httpx.AsyncClient(base_url=httpserver.url_for("")) as client:
        searcher = AsyncSearchClient(client, org="default")
        hits = await searcher.search(sql="SELECT 1", start_time=0, end_time=1, size=1)
    assert hits == []
