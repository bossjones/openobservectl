"""Real-time follow loop for OpenObserve logs.

OpenObserve has no push/subscribe for new logs, so ``tail -f`` is implemented as
**sliding-window polling** on the microsecond ``_timestamp`` column: each poll
queries ``[last_ts, now]`` and advances ``last_ts`` to the newest timestamp seen,
deduplicating records that share the boundary timestamp across polls.

Concurrency follows the classic asyncio producer/consumer pattern: one producer
per stream polls OpenObserve and enqueues hits onto a shared ``asyncio.Queue``;
a single consumer renders them. Multiple streams are tailed concurrently via
``asyncio.gather``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, Protocol

import httpx

__all__ = [
    "AsyncSearchClient",
    "build_tail_sql",
    "follow",
    "now_micros",
    "run_tail",
    "select_new_hits",
]


def now_micros() -> int:
    """Current time as microseconds since epoch (OpenObserve's ``_timestamp`` unit)."""
    return time.time_ns() // 1_000


def _identity(hit: dict[str, Any]) -> str:
    """A stable identity for boundary dedup (order-independent, nested-safe)."""
    return json.dumps(hit, sort_keys=True, default=str)


def _ts(hit: dict[str, Any]) -> int:
    return int(hit.get("_timestamp", 0))


def select_new_hits(
    hits: list[dict[str, Any]],
    last_ts: int,
    seen: set[str],
) -> tuple[list[dict[str, Any]], int, set[str]]:
    """Filter a poll's hits to the genuinely-new ones and advance window state.

    Returns ``(emitted, new_last_ts, new_seen)``. A hit is emitted if its
    ``_timestamp`` is greater than ``last_ts``, or equal to ``last_ts`` but not
    already in ``seen`` (the boundary-dedup set of identities at ``last_ts``).
    """
    emitted: list[dict[str, Any]] = []
    for hit in sorted(hits, key=_ts):
        t = _ts(hit)
        if t > last_ts or (t == last_ts and _identity(hit) not in seen):
            emitted.append(hit)

    if not hits:
        return emitted, last_ts, seen

    new_max = max(_ts(h) for h in hits)
    if new_max > last_ts:
        new_seen = {_identity(h) for h in hits if _ts(h) == new_max}
        return emitted, new_max, new_seen
    # window didn't advance: accumulate any new boundary identities
    merged = set(seen) | {_identity(h) for h in hits if _ts(h) == last_ts}
    return emitted, last_ts, merged


def build_tail_sql(*, stream: str, sql: str | None) -> str:
    """Return the follow query. An explicit ``sql`` wins; else order ``stream`` by ts."""
    if sql:
        return sql
    return f'SELECT * FROM "{stream}" ORDER BY _timestamp ASC'


class _Searcher(Protocol):
    async def search(
        self, *, sql: str, start_time: int, end_time: int, size: int, from_: int = 0
    ) -> list[dict[str, Any]]: ...


async def follow(
    client: _Searcher,
    *,
    stream: str,
    sql: str | None,
    since_micros: int,
    interval: float,
    size: int,
    queue: asyncio.Queue,
    follow: bool = True,
    max_polls: int | None = None,
    stop: asyncio.Event | None = None,
    now_fn: Callable[[], int] = now_micros,
    sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
) -> None:
    """Poll ``client`` for a stream and enqueue new hits.

    With ``follow=False`` this performs a single bounded poll and returns
    (backing ``logs tail`` without ``-f``). With ``follow=True`` it loops on
    ``interval`` until ``stop`` is set, cancelled, or ``max_polls`` is reached.
    """
    query_sql = build_tail_sql(stream=stream, sql=sql)
    last_ts = since_micros
    seen: set[str] = set()
    polls = 0

    while True:
        if stop is not None and stop.is_set():
            break
        end = now_fn()
        hits = await client.search(sql=query_sql, start_time=last_ts, end_time=end, size=size)
        emitted, last_ts, seen = select_new_hits(hits, last_ts, seen)
        for hit in emitted:
            await queue.put(hit)

        polls += 1
        if not follow:
            break
        if max_polls is not None and polls >= max_polls:
            break
        await sleep_fn(interval)


# Module-level alias so run_tail can call the follow() function even though it
# has a boolean parameter also named `follow`.
_poll_stream = follow

_SENTINEL = object()


async def run_tail(
    client: _Searcher,
    *,
    streams: Sequence[str],
    sql: str | None,
    since_micros: int,
    interval: float,
    size: int,
    follow: bool,
    on_hit: Callable[[dict[str, Any]], None],
    stop: asyncio.Event | None = None,
    max_polls: int | None = None,
) -> None:
    """Fan producers (one per stream) into a shared queue drained by ``on_hit``."""
    queue: asyncio.Queue = asyncio.Queue()

    async def _produce() -> None:
        await asyncio.gather(
            *(
                _poll_stream(
                    client,
                    stream=s,
                    sql=sql,
                    since_micros=since_micros,
                    interval=interval,
                    size=size,
                    queue=queue,
                    follow=follow,
                    max_polls=max_polls,
                    stop=stop,
                )
                for s in streams
            )
        )

    producer = asyncio.create_task(_produce())
    producer.add_done_callback(lambda _t: queue.put_nowait(_SENTINEL))

    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            on_hit(item)
    finally:
        if not producer.done():
            producer.cancel()
        # surface a producer error (but not our own cancellation)
        with_result = await asyncio.gather(producer, return_exceptions=True)
        for outcome in with_result:
            if isinstance(outcome, Exception) and not isinstance(outcome, asyncio.CancelledError):
                raise outcome


class AsyncSearchClient:
    """Adapts an httpx.AsyncClient to the _Searcher protocol via POST /api/{org}/_search.

    Mirrors the request/response shape of the existing sync `search` command
    (cli.py) — no `from` key in the body, hits read via data.get("hits", []).
    """

    def __init__(self, client: httpx.AsyncClient, org: str) -> None:
        self._client = client
        self._org = org

    async def search(
        self, *, sql: str, start_time: int, end_time: int, size: int, from_: int = 0
    ) -> list[dict[str, Any]]:
        body = {"query": {"sql": sql, "start_time": start_time, "end_time": end_time, "size": size}}
        resp = await self._client.post(f"/api/{self._org}/_search", json=body)
        resp.raise_for_status()
        data = resp.json()
        return data.get("hits", []) if isinstance(data, dict) else []
