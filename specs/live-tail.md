# Plan: Port live log tailing (`logs tail`) into openobservectl

## Task Description
`openobservectl` was ported from a sibling tool, `ooctl`
(`/Users/bossjones/dev/bossjones/multipass-lab/tools/ooctl`), but the port left
behind `ooctl`'s only real-time feature: tailing logs live across the cluster.
`openobservectl` today is 100% request/response (health/streams/search/query/orgs/
dashboards/config) — there is no follow-mode, no polling loop, no async code at all.
This plan ports `ooctl`'s live-tail engine into `openobservectl` via strict TDD.

## Objective
Add an `openobservectl logs tail` command that fans out sliding-window polling
across one or more OpenObserve log streams and interleaves their live output into
a single terminal feed, matching `ooctl`'s proven `tail.py` design, fully covered
by tests written before the implementation.

## Problem Statement
OpenObserve itself exposes no push/subscribe API, so "tailing" is not a real
streaming connection — it's **sliding-window polling** against the existing
`_search` endpoint, with a dedup algorithm that avoids re-emitting boundary rows.
`ooctl`'s `tail.py` fans one polling coroutine out per **log stream** (not per
node/pod — there's no cluster/k8s discovery involved) into a shared queue drained
by one consumer, so "tail across the cluster" concretely means: auto-discover (or
accept explicit) log streams in one OpenObserve org, and interleave their live
output into one terminal feed.

## Solution Approach
Port `ooctl`'s already-implemented, already-tested engine (`select_new_hits`,
`build_tail_sql`, `follow`, `run_tail`) into a new `src/openobservectl/tail.py`,
adapted to this repo's conventions: reuse the existing `Ctx`/`resolve()`/`_emit`/
`_die` patterns, reuse the existing `_extract_list()` helper for stream discovery,
and add one new adapter (`AsyncSearchClient`) since this repo has no dedicated
REST client class the way `ooctl` does. Introduce `asyncio`/`httpx.AsyncClient`
for this command only — the rest of the CLI stays synchronous. Wire it in as a
new `logs` sub-app (mirrors the existing `dashboards_app`/`config_app` pattern)
with an initial `logs tail` subcommand.

**Decisions locked in (from user clarification), do not revisit:**
1. "Across the cluster" = multi-**stream** fan-out within one org (matches `ooctl` exactly), not multi-OpenObserve-endpoint fan-out.
2. Introduce `asyncio`/`httpx.AsyncClient` for this command only; rest of the CLI stays synchronous. Add `pytest-asyncio` as a dev dependency.
3. New `logs` sub-app (mirrors the existing `dashboards_app`/`config_app` pattern) with an initial `logs tail` subcommand — leaves room for a future `logs search`.

**Explicitly out of scope for this plan** (non-goals):
- `logs search` / SSE streaming search (`ooctl`'s `sse.py` — not needed; `tail` uses polling, not SSE).
- Multi-endpoint/multi-cluster fan-out.
- Cross-stream timestamp-sorted merge (output stays strict queue-arrival order, exactly like `ooctl`).
- Source/stream-name line prefixing or per-source coloring (optional stretch only).
- Per-stream error isolation/reconnect/backoff (one stream's error aborts the whole tail, same limitation as `ooctl` — `asyncio.gather` fails fast; fixing this changes cancellation semantics, not worth it here).
- Automated live-cluster integration test (optional stretch; manual verification is the required check).

## Relevant Files
Use these files to complete the task:

- `src/openobservectl/cli.py` — all commands + `Ctx`/`Options`/`resolve()`/`_emit`/`_die` live here; add `Ctx.async_client()`, the `logs_app` sub-app, and `logs tail`. Reuse `_extract_list()` (already defined here for dashboards) for stream-list parsing.
- `src/openobservectl/common.py` — stdlib-only helpers; add `parse_duration_seconds()` here (pure, no `time.time()` call inside, unlike ooctl's version — keeps it independently unit-testable).
- `tests/test_cli.py` — existing `_server(httpserver, streams=...)` / `_run(base, *args)` helpers (test_cli.py:19-27) to reuse verbatim for new CLI-level tests.
- `tests/test_common.py` — add `parse_duration_seconds` tests here.
- `pyproject.toml` — add `pytest-asyncio` dev dep, `asyncio_mode = "auto"`, an `integration` pytest marker deselected by default.
- Reference implementation to port from (read, don't modify): `/Users/bossjones/dev/bossjones/multipass-lab/tools/ooctl/src/ooctl/tail.py` (189 lines — core engine: `select_new_hits`, `build_tail_sql`, `now_micros`, `follow`, `run_tail`) and `.../ooctl/src/ooctl/render.py` (log-level colorized rendering, multi-key schema fallback, markup escaping).

### New Files
- `src/openobservectl/tail.py` — ported engine (`select_new_hits`, `build_tail_sql`, `now_micros`, `follow`, `run_tail`, plus a new `AsyncSearchClient` adapter — `ooctl` has a whole `client.py`/`OpenObserveClient` class this repo doesn't have and shouldn't add; instead adapt `httpx.AsyncClient` directly to the same `_Searcher` protocol via one small class).
- `src/openobservectl/render.py` — ported log-line rendering (`format_timestamp`, `render_hit`, `write_hit`).
- `tests/test_tail.py` — new; pure-function + async engine tests (mirrors `ooctl/tests/test_tail.py`).
- `tests/test_render.py` — new; mirrors `ooctl/tests/test_render.py`.

## Implementation Phases

### Phase 1: Foundation
Dependency/config changes (`pyproject.toml`), then the pure, HTTP-free core of the tail engine (`select_new_hits`, `build_tail_sql`, `now_micros`) with no async and no I/O — the safest, most mechanical porting work, fully unit-testable in isolation.

### Phase 2: Core Implementation
The async engine (`follow`, `run_tail`) with injected `now_fn`/`sleep_fn` for deterministic tests, then the one piece of new glue code this repo needs that `ooctl` didn't (`AsyncSearchClient`, since `ooctl` has a dedicated REST client class and this repo builds requests ad hoc per-command), then the `--since` duration parser, then `render.py`.

### Phase 3: Integration & Polish
Wire `logs tail` into `cli.py`, add CLI-level hermetic tests via `pytest-httpserver`, run `just check`, then manually verify against a real/local OpenObserve instance.

## Step by Step Tasks
IMPORTANT: Execute every step in order, top to bottom.

### 1. Add TDD/async test infrastructure
- In `pyproject.toml`: add `"pytest-asyncio>=0.24"` to `[dependency-groups] dev`.
- Add `asyncio_mode = "auto"` to `[tool.pytest.ini_options]` (lets `async def test_...` run undecorated, matching `ooctl`'s test style).
- Add an `integration` marker, deselected by default: `addopts = "-ra -m 'not integration'"` plus a `markers = ["integration: opt-in live tests against a running OpenObserve (deselected by default)"]` entry.
- Run `uv sync`.

### 2. Port pure dedup/windowing functions (`select_new_hits`) — TDD
- Write `tests/test_tail.py` first, importing `from openobservectl.tail import select_new_hits`:
  - `_hit(ts, msg) -> {"_timestamp": ts, "log": msg}` helper.
  - `test_select_all_new_on_first_window` — unsorted input emits sorted-by-ts, `last_ts` advances to max.
  - `test_empty_poll_leaves_state_unchanged`.
  - `test_boundary_record_not_re_emitted_across_polls` — two-poll sequence where a record at the exact boundary timestamp is deduped on the second poll.
  - `test_two_records_sharing_max_ts_both_emitted_then_deduped` — tie-at-max-ts across 3 polls.
- Implement in `src/openobservectl/tail.py`: `_identity(hit)` (json.dumps sort_keys), `_ts(hit)`, `select_new_hits(hits, last_ts, seen) -> (emitted, new_last_ts, new_seen)` — port from `ooctl/src/ooctl/tail.py:36-71`, adapting only names/imports.

### 3. Port `build_tail_sql` and `now_micros` — TDD
- Add to `tests/test_tail.py`: `test_build_tail_sql_default_orders_by_timestamp` (contains `FROM "<stream>"` and `_timestamp`), `test_build_tail_sql_uses_explicit_sql` (returns the raw override verbatim), `test_now_micros_returns_a_plausible_epoch_microsecond_value` (sanity bound, catches ms/µs mistakes).
- Implement `build_tail_sql(*, stream, sql)` and `now_micros()` in `tail.py` (port from `ooctl/tail.py:31-33,74-78`).

### 4. Port the async `follow()` polling loop — TDD
- Add to `tests/test_tail.py`: a `StubClient` test double (`async def search(self, *, sql, start_time, end_time, size, from_=0)` — records `(start_time, end_time)` call tuples, pops scripted pages).
  - `test_follow_single_poll_emits_hits` (`follow=False`).
  - `test_follow_advances_window_and_dedups_across_polls` (`max_polls=2`, injected fake `sleep_fn` recording calls instead of really sleeping — assert the second poll's `start_time` equals the first poll's max timestamp, and that `sleep_fn` was actually invoked between polls).
  - `test_follow_respects_stop_event` (pre-set `asyncio.Event`, assert loop exits promptly without sleeping) — new test, not in `ooctl`, added because we're relying on `stop` for clean shutdown.
- Implement `follow(...)` in `tail.py` — port from `ooctl/tail.py:87-127` verbatim, including the `_Searcher` Protocol and the `now_fn`/`sleep_fn` injection points (critical: do not drop these, they're what makes the tests fast and deterministic).

### 5. Port `run_tail()` fan-out — TDD
- Add to `tests/test_tail.py`:
  - `test_run_tail_fans_out_streams_to_a_shared_consumer` — two streams via `StubClient`, assert the consumer received hits from both by **set** equality (not strict order — matches `ooctl`'s documented queue-arrival-order design).
  - `test_run_tail_propagates_a_producer_exception` — one stream's `search()` raises; assert `run_tail(...)` propagates (locks in the accepted single-stream-failure-aborts-all limitation rather than silently swallowing it).
- Implement `run_tail(...)` in `tail.py` — port from `ooctl/tail.py:130-189` (`_SENTINEL`, producer task + `add_done_callback`, single consumer loop, `finally`-block cancel+gather cleanup).
- At this point `tail.py`'s engine is fully ported and tested with zero HTTP/Typer dependency.

### 6. Add `AsyncSearchClient` — the one new adapter this repo needs
- Add to `tests/test_tail.py` (hermetic, via `pytest-httpserver` + a real `httpx.AsyncClient`):
  - `test_async_search_client_posts_expected_body_and_parses_hits` — asserts the POST body shape matches the **existing sync `search` command's** shape exactly (`cli.py:238-246`: `{"query": {"sql", "start_time", "end_time", "size"}}`, no `from` key) and that hits are read via `data.get("hits", [])`.
  - `test_async_search_client_returns_empty_list_when_hits_missing`.
- Implement `AsyncSearchClient` in `tail.py`: wraps an `httpx.AsyncClient` + org, implements the `_Searcher.search()` protocol via `POST /api/{org}/_search`, reusing the exact request/response convention already established by the sync `search` command — do not invent a new body shape or a `SearchError` exception type (unlike `ooctl`'s dedicated `client.py`); let `httpx.HTTPError` propagate and get caught by the CLI's existing `_die` convention.

### 7. Add `Ctx.async_client()`
- No dedicated unit test (one-liner, exercised by Step 9's CLI tests).
- In `cli.py`, right after the existing `Ctx.client()` (cli.py:67-73), add `async_client(self) -> httpx.AsyncClient` building an `httpx.AsyncClient` with the identical `base_url`/`auth`/`timeout`/`verify` args as `.client()`.

### 8. Add `--since` duration parsing — TDD
- Add to `tests/test_common.py`:
  - `test_parse_duration_seconds_parses_s_m_h_d` — parametrized `30s`→30, `5m`→300, `1h`→3600, `2d`→172800.
  - `test_parse_duration_seconds_rejects_bad_format` — `pytest.raises(ValueError)` for `"5"`, `"5x"`, `"m5"`, `""`.
- Implement `parse_duration_seconds(text: str) -> int` in `src/openobservectl/common.py` (add `import re`) — a regex `^\s*(\d+)\s*([smhd])\s*$`, raising `ValueError` with a helpful message on mismatch. Deliberately pure (no internal `now()` call, unlike `ooctl`'s `_parse_since`) so it unit-tests cleanly; the CLI computes `since_micros = otail.now_micros() - parse_duration_seconds(since) * 1_000_000` itself.

### 9. Port `render.py` — TDD
- Add `tests/test_render.py` (port `ooctl/tests/test_render.py`): timestamp formatting (micros→ISO UTC, zero/missing→dash), `render_hit(..., as_json=True)` produces a single-line JSON that round-trips, `render_hit(..., as_json=False)` contains timestamp/level/message, message-field fallback order (`message`→`log`→`body`→`msg`), tolerance for a missing timestamp.
- Implement `src/openobservectl/render.py` — port `ooctl/src/ooctl/render.py` verbatim (`format_timestamp`, `_first`, `_message`, `render_hit`, `write_hit`, `_LEVEL_COLORS`, `rich.markup.escape` on log bodies so `[...]`-containing lines don't break terminal rendering). Keep `render_hit(as_json=True)`'s compact single-line JSON separate from `common.print_json`'s indented multi-doc dump — different jobs, don't merge them.

### 10. Wire `logs tail` into the CLI — TDD
- Extend `tests/test_cli.py` using the existing `_server(httpserver, streams=...)` / `_run(base, *args)` helpers (test_cli.py:19-27):
  - `test_logs_tail_without_follow_single_window` — explicit `--stream`, one poll, hit text appears in output.
  - `test_logs_tail_autodiscovers_logs_streams` — no `--stream`/`--sql`; asserts the discovered stream name appears in the (non-JSON) status line and the hit renders.
  - `test_logs_tail_no_logs_streams_exits_nonzero` — empty stream list, `--stream`/`--sql` omitted, exit code nonzero, "no logs streams" in output.
  - `test_logs_tail_sql_override_skips_autodiscovery` — `--sql` given, `streams` list in `_server()` is empty, must not error (auto-discovery must not fire).
  - `test_logs_tail_json_flag_emits_compact_json_lines_only` — under `--json`, the `"tailing: ..."` status line must NOT appear (this is a deliberate deviation from `ooctl`, which always prints it — keep `--json` output pure/jq-able), and the last output line parses as JSON with the expected `log` field.
  - `test_logs_tail_invalid_since_exits_nonzero` — bad `--since` value, nonzero exit, "invalid" in output.
- Implement in `cli.py`:
  1. Add `import asyncio` to the top-level imports.
  2. Add `from openobservectl import tail as otail` and `from openobservectl import render as orender`.
  3. Lightly refactor the existing `streams()` command (cli.py:212-225) to use the existing `_extract_list(data, "list")` helper instead of its current inline conditional — run `test_streams_lists_names`/`test_streams_sends_basic_auth` after this change to confirm no regression, before adding new code.
  4. Add a new `logs_app = typer.Typer(...)` sub-app + `app.add_typer(logs_app, name="logs")`, placed after `orgs()` and before the dashboards section.
  5. Add `@logs_app.command("tail")` with flags: `--stream` (repeatable, `list[str]`), `-f`/`--follow` (bool), `--since` (str, default `"5m"`), `--sql` (str, optional), `--interval` (float, default `2.0`), `--limit` (int, default `200`).
  6. Command body: resolve `Ctx` via `resolve(ctx.obj)`; compute `since_micros` via `otail.now_micros() - oc.parse_duration_seconds(since) * 1_000_000`, catching `ValueError` → `_die(str(exc))`; define `_on_hit(hit)` calling `orender.write_hit(console, hit, as_json=c.as_json)`; define an inner `async def _run()` that opens `c.async_client()`, builds `otail.AsyncSearchClient(client, c.org)`, resolves the stream list (explicit `--stream` > `--sql` as pseudo-stream `"query"` > auto-discovery via `GET /api/{org}/streams?type=logs` + `_extract_list(..., "list")`, dying with code 2 if none found), prints the `"tailing: ..."` status line only when `not c.as_json`, then `await otail.run_tail(...)`; wrap `asyncio.run(_run())` in `except KeyboardInterrupt: console.print("\n[dim]stopped[/dim]")` and `except httpx.HTTPError as exc: _die(f"tail failed: {exc}")`.
- Run `just check` (ruff format/check + basedpyright + codespell + pytest) and fix anything it flags.

### 11. Manual verification (required)
- Against a real/local OpenObserve (via `--lab-root` pointing at a multipass-lab checkout, or a local `docker run` OpenObserve instance):
  - `openobservectl logs tail --stream <name> --since 10m` — single window, prints hits, exits.
  - `openobservectl logs tail -f --since 1m --interval 2` — live follow; Ctrl-C prints `stopped` and exits cleanly with no traceback.
  - `openobservectl logs tail --since 5m` (no `--stream`/`--sql`) — confirms auto-discovery status line and multi-stream fan-out if more than one logs stream exists.
  - `openobservectl --json logs tail --since 5m | jq .` — confirm every line parses as JSON, no stray status line.
  - `openobservectl logs tail --sql "SELECT * FROM some_stream WHERE level = 'ERROR'" -f` — confirms the `--sql` override path.

## Testing Strategy
Follow this repo's existing TDD convention exactly (test docstrings already say "(TDD)"): write the failing test first, then the minimal code to pass it, in the order above — pure functions (`select_new_hits`, `build_tail_sql`, `now_micros`, `parse_duration_seconds`) before anything async, async engine tests with injected `now_fn`/`sleep_fn` before any real HTTP, one hermetic `AsyncSearchClient` test via `pytest-httpserver` before CLI wiring, and CLI-level `CliRunner` + `pytest-httpserver` tests last. This ordering means every layer is independently verified before the next layer depends on it — by the time `logs tail` is wired into `cli.py`, the engine underneath it is already fully proven.

Edge cases covered by the test list above: empty poll (no new hits), boundary-timestamp dedup, tied-max-timestamp handling, `stop`-event early exit, multi-stream fan-out ordering (set-based, not strict), single-stream producer failure aborting the whole tail (a documented limitation, not a bug), no-streams-found exit code, `--sql` bypassing auto-discovery, `--json` output purity, and invalid `--since` input.

## Acceptance Criteria
- `openobservectl logs tail` exists with flags `--stream` (repeatable), `-f`/`--follow`, `--since`, `--sql`, `--interval`, `--limit`, matching `ooctl`'s flag set (no `--json` porting needed as a new flag — the existing global `--json` option already applies).
- Without `--stream`/`--sql`, the command auto-discovers logs streams and fans out polling across all of them concurrently, merging output into one live feed.
- `-f`/`--follow` keeps polling until Ctrl-C, printing `stopped` on interrupt with no traceback; without `-f`, a single bounded window is fetched and the command exits.
- `--json` output is clean, single-JSON-object-per-line, with no extraneous status text.
- All new pure functions and the async engine have unit tests with no real I/O or real sleeping; CLI wiring has hermetic `pytest-httpserver`-backed tests.
- `just check` passes (ruff format/check, basedpyright, codespell, pytest).
- Manual verification against a real OpenObserve instance confirms both single-window and live-follow behavior, and clean Ctrl-C shutdown.

## Validation Commands
Execute these commands to validate the task is complete:

- `cd /Users/bossjones/dev/bossjones/openobservectl && uv sync` — install the new `pytest-asyncio` dev dependency.
- `just check` — ruff format-check, ruff lint, basedpyright, codespell, full pytest suite (equivalent to CI).
- `uv run pytest tests/test_tail.py tests/test_render.py -v` — focused run of the new engine/render tests.
- `uv run pytest tests/test_cli.py -k logs_tail -v` — focused run of the new CLI-level tail tests.
- `uv run openobservectl logs tail --help` — confirm flag wiring/help text renders correctly.
- Manual verification commands from Step 11 above, against a real/local OpenObserve instance.

## Notes
- New dev dependency: `pytest-asyncio>=0.24` (add via `uv add --dev pytest-asyncio` or directly in `pyproject.toml`'s `[dependency-groups] dev`). No new runtime dependency — `httpx>=0.27` (already present) provides `AsyncClient` natively.
- This plan is intentionally scoped to `logs tail` only. `ooctl`'s `logs search` (true SSE streaming for one-shot queries) is a natural follow-up once this ships, reusing the same `logs_app` sub-app, but is explicitly out of scope here.
