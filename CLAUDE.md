# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`openobservectl` is a Typer CLI that verifies, introspects, and provisions dashboards on an
[OpenObserve](https://openobserve.ai/) instance over its REST API. OpenObserve ships no read SDK,
so everything talks to the REST API directly with `httpx`. The headline use case is a CI-friendly
`check` command that exits nonzero when the instance is unhealthy or auth fails.

## Commands

Everything runs through `uv` and `just` (see `justfile`):

```bash
uv sync                 # install deps + create .venv
just check              # full CI gate: lint + test (this is `default`)
just lint               # ruff format --check + ruff check + basedpyright + codespell
just fmt                # ruff format . (apply formatting)
just test               # pytest
just help               # smoke-test both entrypoints (`openobservectl` and `python -m`)
```

Run a single test:

```bash
uv run pytest tests/test_tail.py                          # one file
uv run pytest tests/test_tail.py::test_select_new_hits    # one test
uv run pytest -k dedup                                    # by name substring
```

`pytest` deselects the `integration` marker by default (`addopts = -ra -m 'not integration'`).
Integration tests hit a live OpenObserve — opt in with `uv run pytest -m integration`. Tests are
otherwise **hermetic**: they use `pytest-httpserver` to fake the OpenObserve API, and
`pytest-randomly` shuffles order, so tests must not depend on ordering.

## Server / credential resolution

The single most important thing to understand is the resolution precedence, implemented in
`cli.py::resolve()`. When touching any connection logic, preserve this order:

- **Base URL**: `--server-url` > `$OPENOBSERVE_URL` > profile `endpoint` > `tofu output` (via
  `--lab-root`/`$MULTIPASS_LAB_ROOT`, the multipass-lab fallback). If none resolve, exit nonzero.
- **Credentials**: `--user`/`--password` > `$OPENOBSERVE_USER`/`$OPENOBSERVE_PASSWORD` > profile >
  built-in defaults (`admin@example.com` / `Complexpass#123`).
- **Org**: `--org` (if not the literal default `"default"`) > profile `organization` > `"default"`.

Profiles live in `~/.openobservectl/config.yaml` (override path via `--config`/`$OPENOBSERVECTL_CONFIG`;
select via `--profile`/`$OPENOBSERVECTL_PROFILE`; the `default` profile auto-loads if the file exists
and no profile was requested). An explicitly requested profile that fails to load is a hard error;
an implicitly discovered one silently falls through to the next source.

## Architecture

The package is deliberately layered so the non-CLI logic stays testable in isolation:

- **`common.py`** — stdlib-only helpers (no rich/typer/httpx). OpenTofu output parsing, the
  flag>env>default resolution primitives, a `urllib`-based `http_get_json`, `poll()` readiness
  loop, `parse_duration_seconds` (`30s`/`5m`/`1h`/`2d`), and the `CheckReport` accumulator that
  derives the `check` exit code (`CHECK_FAIL_EXIT = 2`). Keep this module import-light.
- **`config.py`** — Pydantic `Profile`/`Config` models, YAML loading, and `resolve_profile()` which
  re-validates through the model after applying `OPENOBSERVE_*` env overrides so field validators
  (e.g. endpoint trailing-slash stripping) run on overridden values too.
- **`render.py`** — turns a single log hit into a terminal line. `write_hit` colors by level;
  `--json` mode bypasses rich's Console entirely (plain `print`) so JSON output is never wrapped
  or highlighted. Log bodies are `rich.markup.escape`-d (they may contain `[..]` markup).
- **`tail.py`** — async follow loop. OpenObserve has no push/subscribe, so `logs tail` is
  **sliding-window polling** on the microsecond `_timestamp` column: each poll queries
  `[last_ts, now]`, advances `last_ts` to the newest timestamp, and dedups records sharing the
  boundary timestamp across polls (`select_new_hits` holds the window-state logic — the trickiest
  code in the repo, test it directly). Classic asyncio producer/consumer: one producer per stream
  enqueues onto a shared `asyncio.Queue`, one consumer renders; multiple streams via `asyncio.gather`.
- **`cli.py`** — Typer glue only. The root `@app.callback` stashes raw flags into an `Options`
  dataclass on `ctx.obj`; each command calls `resolve()` to produce a `Ctx` (base_url, creds, org,
  output prefs) and a configured `httpx.Client`/`AsyncClient`. Subcommand groups: `logs` (tail),
  `dashboards` (list/import/delete), `config` (path/list).

Two HTTP stacks coexist by design: `common.http_get_json` uses stdlib `urllib` (only for the tofu
resolution path); all actual OpenObserve commands use `httpx` (sync `Client` for one-shot commands,
async `AsyncClient` for `logs tail`).

## Conventions specific to this codebase

- **Tolerant response extraction.** OpenObserve's response/payload shapes vary across versions (the
  deployment image is `:latest`). Extraction helpers (`_extract_list`, `_dash_title`, `_dash_id`,
  `_folder_id`) probe multiple keys and nested version objects (e.g. dashboards nested under a `v5`
  key). Follow the existing `data.get("list", data)` idiom — surface the raw body rather than an
  empty list when the expected key is absent, so HTTP-200 error payloads stay visible.
- **Dashboards ship as package data** under `src/openobservectl/dashboards/<Folder>/*.json` (12
  dashboards across `Correlation/`, `Infrastructure/`, `LogAnalysis/`), located via
  `importlib.resources`. `dashboards import` and `check --require-dashboards` default to these.
  Import is idempotent, upserting by dashboard **title** (POST to create, PUT with the existing
  `hash` to update). Folders use a v2 API path (`/api/v2/{org}/folders/dashboards`) while dashboards
  themselves stay on v1.
- **`check` is composite and additive.** Health + auth always run; `--require-streams`,
  `--require-metrics`, `--require-logs`, `--require-dashboards` add assertions. Non-required checks
  are reported as informational `skip`. Any failure → exit `2`.
- **`_die(msg, code)`** is the single error exit path in `cli.py`; commands catch `httpx.HTTPError`
  and route through it rather than letting tracebacks escape.

## Tooling

Python `>=3.11,<4.0`. Linting: `ruff` (line-length 100; `E501`/`B008` ignored — `B008` because
Typer relies on `typer.Option(...)` calls in argument defaults). Type checking: `basedpyright`
(standard mode, `src` only). Spelling: `codespell`. Version is derived from git tags via
`uv-dynamic-versioning` — there is no hardcoded version string.
