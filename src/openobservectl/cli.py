"""openobservectl — verify & introspect an OpenObserve instance over its REST API.

OpenObserve ships no read SDK, so this talks to the REST API directly with httpx.
Introspection (rich tables or `--json`) plus a CI-friendly `check` that asserts
OpenObserve is healthy and authentication works. Resolves the server URL in this
precedence: `--server-url` > `$OPENOBSERVE_URL` > a `~/.openobservectl/config.yaml`
profile > `tofu output` (via `--lab-root`, for a multipass-lab checkout).

    openobservectl check
    openobservectl streams --json
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, NoReturn

import httpx
import typer
from rich.console import Console
from rich.table import Table

from openobservectl import common as oc
from openobservectl import config as ocfg
from openobservectl import render as orender
from openobservectl import tail as otail

PORT = 5080
DEFAULT_USER = "admin@example.com"
DEFAULT_PASSWORD = "Complexpass#123"

# Dashboards ship as package data under src/openobservectl/dashboards/<Folder>/*.json.
DASHBOARDS_DIR = Path(str(files("openobservectl") / "dashboards"))

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()


@dataclass
class Options:
    cluster: str
    server_url: str | None
    user: str | None
    password: str | None
    org: str
    as_json: bool
    timeout: float
    insecure: bool
    profile: str | None
    config: str | None
    lab_root: str | None


@dataclass
class Ctx:
    base_url: str
    user: str
    password: str
    org: str
    as_json: bool
    timeout: float
    insecure: bool

    def _client_kwargs(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "auth": (self.user, self.password),
            "timeout": self.timeout,
            "verify": not self.insecure,
        }

    def client(self) -> httpx.Client:
        return httpx.Client(**self._client_kwargs())

    def async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(**self._client_kwargs())


@app.callback()
def _main(
    ctx: typer.Context,
    cluster: str = typer.Option("centralized_monitoring", "--cluster"),
    server_url: str = typer.Option(None, "--server-url", help="override; else profile/tofu"),
    user: str = typer.Option(None, "--user", help="default admin@example.com / $OPENOBSERVE_USER"),
    password: str = typer.Option(None, "--password", help="$OPENOBSERVE_PASSWORD"),
    org: str = typer.Option("default", "--org"),
    as_json: bool = typer.Option(False, "--json", help="machine-readable JSON output"),
    timeout: float = typer.Option(10.0, "--timeout"),
    insecure: bool = typer.Option(False, "--insecure"),
    profile: str = typer.Option(
        None, "--profile", "-p", help="~/.openobservectl/config.yaml profile"
    ),
    config: str = typer.Option(None, "--config", help="config file path ($OPENOBSERVECTL_CONFIG)"),
    lab_root: str = typer.Option(
        None, "--lab-root", help="multipass-lab checkout for tofu fallback ($MULTIPASS_LAB_ROOT)"
    ),
):
    """OpenObserve verification CLI."""
    ctx.obj = Options(
        cluster,
        server_url,
        user,
        password,
        org,
        as_json,
        timeout,
        insecure,
        profile,
        config,
        lab_root,
    )


def _die(msg: str, code: int = 1) -> NoReturn:
    console.print(f"[red]error:[/red] {msg}")
    raise typer.Exit(code)


def resolve(opts: Options) -> Ctx:
    cfg_path = (
        opts.config or os.environ.get("OPENOBSERVECTL_CONFIG") or str(ocfg.default_config_path())
    )
    name = opts.profile or os.environ.get("OPENOBSERVECTL_PROFILE")
    profile: ocfg.Profile | None = None
    if name or (opts.profile is None and Path(cfg_path).exists()):
        try:
            profile = ocfg.resolve_profile(ocfg.load_config(cfg_path), name or "default")
        except ocfg.ConfigError as exc:
            if opts.profile or name:  # explicit request must not silently fall through
                _die(str(exc))

    # base URL precedence: --server-url > $OPENOBSERVE_URL > profile.endpoint > tofu(--lab-root)
    server_url = (
        opts.server_url
        or os.environ.get("OPENOBSERVE_URL")
        or (profile.endpoint if profile else None)
    )
    if server_url:
        base_url = server_url.rstrip("/")
    elif opts.lab_root or os.environ.get("MULTIPASS_LAB_ROOT"):
        lab_root = opts.lab_root or os.environ["MULTIPASS_LAB_ROOT"]
        target = oc.resolve_target(port=PORT, chdir=oc.default_chdir(opts.cluster, lab_root))
        base_url = target.base_url
    else:
        _die(
            "no OpenObserve URL: pass --server-url, set $OPENOBSERVE_URL, "
            "configure a profile in ~/.openobservectl/config.yaml, or point "
            "--lab-root at a multipass-lab checkout for the tofu fallback"
        )

    # credentials: flags > env > profile > built-in defaults
    user, password = oc.resolve_credentials(
        opts.user,
        opts.password,
        user_env="OPENOBSERVE_USER",
        pass_env="OPENOBSERVE_PASSWORD",
        default_user=(profile.username if profile else DEFAULT_USER),
        default_password=(profile.password if profile else DEFAULT_PASSWORD),
    )
    org = opts.org if opts.org != "default" else (profile.organization if profile else "default")
    return Ctx(
        base_url=base_url,
        user=user,
        password=password,
        org=org,
        as_json=opts.as_json,
        timeout=opts.timeout,
        insecure=opts.insecure,
    )


def _json_or_text(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 - non-JSON body (e.g. plain-text /healthz)
        return {"status_code": resp.status_code, "text": resp.text}


def _emit(c: Ctx, data: Any, columns: list[str] | None = None, title: str | None = None) -> None:
    if c.as_json:
        oc.print_json(data)
        return
    if isinstance(data, list) and data and isinstance(data[0], dict):
        cols = columns or list(data[0].keys())
        table = Table(*cols, title=title)
        for row in data:
            table.add_row(*[str(row.get(col, "")) for col in cols])
        console.print(table)
    elif isinstance(data, dict):
        table = Table("field", "value", title=title)
        for key, value in data.items():
            table.add_row(str(key), str(value))
        console.print(table)
    else:
        console.print(str(data))


# --- introspection commands --------------------------------------------------


@app.command()
def health(ctx: typer.Context):
    """Liveness probe (GET /healthz)."""
    c = resolve(ctx.obj)
    try:
        with c.client() as client:
            resp = client.get("/healthz")
            resp.raise_for_status()
            data = _json_or_text(resp)
    except httpx.HTTPError as exc:
        _die(f"openobserve unreachable: {exc}")
    _emit(c, data, title="openobserve health")


@app.command()
def streams(ctx: typer.Context, type_: str = typer.Option(None, "--type")):
    """List ingest streams (GET /api/{org}/streams)."""
    c = resolve(ctx.obj)
    params = {"type": type_} if type_ else None
    try:
        with c.client() as client:
            resp = client.get(f"/api/{c.org}/streams", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        _die(f"could not list streams: {exc}")
    # Show the raw body (not an empty list) when "list" is absent — e.g. an HTTP-200
    # error payload like {"code": 403, "message": ...} — so the problem stays visible.
    rows = data.get("list", data) if isinstance(data, dict) else data
    _emit(c, rows, title="streams")


@app.command()
def search(
    ctx: typer.Context,
    sql: str = typer.Argument(...),
    start: int = typer.Option(None, "--start", help="start time (µs epoch)"),
    end: int = typer.Option(None, "--end", help="end time (µs epoch)"),
    size: int = typer.Option(100, "--size"),
):
    """Run a SQL search over a stream (POST /api/{org}/_search)."""
    c = resolve(ctx.obj)
    now_us = otail.now_micros()
    body = {
        "query": {
            "sql": sql,
            "start_time": start if start is not None else now_us - 3_600_000_000,
            "end_time": end if end is not None else now_us,
            "size": size,
        }
    }
    try:
        with c.client() as client:
            resp = client.post(f"/api/{c.org}/_search", json=body)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        _die(f"search failed: {exc}")
    _emit(c, data.get("hits", data), title="search hits")


@app.command()
def query(ctx: typer.Context, promql: str = typer.Argument(...)):
    """PromQL-compatible metrics query (GET /api/{org}/prometheus/api/v1/query)."""
    c = resolve(ctx.obj)
    try:
        with c.client() as client:
            resp = client.get(f"/api/{c.org}/prometheus/api/v1/query", params={"query": promql})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        _die(f"query failed: {exc}")
    oc.print_json(data) if c.as_json else _emit(c, data, title=f"query: {promql}")


@app.command()
def orgs(ctx: typer.Context):
    """List organizations (GET /api/organizations)."""
    c = resolve(ctx.obj)
    try:
        with c.client() as client:
            resp = client.get("/api/organizations")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        _die(f"could not list orgs: {exc}")
    _emit(c, data.get("data", data) if isinstance(data, dict) else data, title="orgs")


# --- logs (tail) --------------------------------------------------------------
# OpenObserve has no push/subscribe API for new logs, so `logs tail` is sliding-window
# polling on `_timestamp` via the existing `_search` endpoint (see openobservectl/tail.py).

logs_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Tail logs.")
app.add_typer(logs_app, name="logs")


@logs_app.command("tail")
def logs_tail(
    ctx: typer.Context,
    stream: list[str] = typer.Option(None, "--stream", help="stream(s) to tail (repeatable)"),
    follow: bool = typer.Option(False, "-f", "--follow", help="keep following new logs"),
    since: str = typer.Option("5m", "--since", help="initial lookback, e.g. 30s/5m/1h/2d"),
    sql: str = typer.Option(None, "--sql", help="raw SQL (overrides --stream ordering)"),
    interval: float = typer.Option(2.0, "--interval", help="poll interval seconds (with -f)"),
    limit: int = typer.Option(200, "--limit", help="max rows per poll"),
):
    """Tail logs via sliding-window polling on `_timestamp` (no server push API)."""
    if stream and sql:
        _die("--stream and --sql cannot be combined; pass one or the other", code=2)
    c = resolve(ctx.obj)
    try:
        since_micros = otail.now_micros() - oc.parse_duration_seconds(since) * 1_000_000
    except ValueError as exc:
        _die(str(exc))

    def _on_hit(hit: Any) -> None:
        orender.write_hit(console, hit, as_json=c.as_json)

    async def _run() -> None:
        async with c.async_client() as client:
            searcher = otail.AsyncSearchClient(client, c.org)
            if stream:
                streams_ = list(stream)
            elif sql:
                streams_ = ["query"]
            else:
                resp = await client.get(f"/api/{c.org}/streams", params={"type": "logs"})
                resp.raise_for_status()
                rows = _extract_list(resp.json(), "list")
                streams_ = [r["name"] for r in rows if isinstance(r, dict) and r.get("name")]
                if not streams_:
                    _die("no logs streams found to tail; pass --stream or --sql", code=2)
                if not c.as_json:
                    console.print(f"[dim]tailing: {', '.join(streams_)}[/dim]")
            await otail.run_tail(
                searcher,
                streams=streams_,
                sql=sql,
                since_micros=since_micros,
                interval=interval,
                size=limit,
                follow=follow,
                on_hit=_on_hit,
            )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")
    except httpx.HTTPError as exc:
        _die(f"tail failed: {exc}")


# --- dashboards --------------------------------------------------------------
# OpenObserve stores dashboards in folders and exposes /api/{org}/{folders,dashboards}.
# Response + payload shapes vary across OpenObserve versions (the image is :latest), so
# extraction is deliberately tolerant — mirror the existing `data.get("list", data)` idiom.

dashboards_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Manage OpenObserve dashboards."
)
app.add_typer(dashboards_app, name="dashboards")


def _extract_list(data: Any, *keys: str) -> list[Any]:
    """Pull a list out of a response under any of `keys`, else the first list value."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if key in data:
                return data[key] or []
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


def _dash_title(item: Any) -> str | None:
    """Best-effort dashboard title (may be nested under a version key, e.g. `v5`)."""
    if not isinstance(item, dict):
        return None
    if item.get("title"):
        return item["title"]
    for value in item.values():
        if isinstance(value, dict) and value.get("title"):
            return value["title"]
    return None


def _dash_id(item: Any) -> str | None:
    """Best-effort dashboard id across schema versions."""
    if not isinstance(item, dict):
        return None
    for key in ("dashboardId", "dashboard_id", "id"):
        if item.get(key):
            return item[key]
    for value in item.values():
        if isinstance(value, dict):
            for key in ("dashboardId", "dashboard_id", "id"):
                if value.get(key):
                    return value[key]
    return None


def _folder_id(item: Any) -> str | None:
    return item.get("folderId") or item.get("folder_id") if isinstance(item, dict) else None


def _list_dashboards(client: httpx.Client, org: str, folder: str = "default") -> list[Any]:
    resp = client.get(f"/api/{org}/dashboards", params={"folder": folder})
    resp.raise_for_status()
    return _extract_list(resp.json(), "dashboards", "list")


def _folders_path(org: str) -> str:
    # v0.91+: dashboard folders live under the v2 API (dashboards themselves stay on v1).
    return f"/api/v2/{org}/folders/dashboards"


def _folders(client: httpx.Client, org: str) -> list[tuple[str, str]]:
    """Return [(name, folderId)] including the implicit `default` folder."""
    out = [("default", "default")]
    resp = client.get(_folders_path(org))
    resp.raise_for_status()
    for f in _extract_list(resp.json(), "list", "folders"):
        fid = _folder_id(f)
        if fid and fid != "default":
            out.append((f.get("name") or fid, fid))
    return out


def _ensure_folder(client: httpx.Client, org: str, name: str) -> str:
    """Return the folderId for `name`, creating the folder if absent. `default` is implicit."""
    if not name or name == "default":
        return "default"
    resp = client.get(_folders_path(org))
    resp.raise_for_status()
    for f in _extract_list(resp.json(), "list", "folders"):
        if isinstance(f, dict) and f.get("name") == name:
            return _folder_id(f) or "default"
    resp = client.post(_folders_path(org), json={"name": name, "description": ""})
    resp.raise_for_status()
    return _folder_id(resp.json()) or "default"


def _installed_titles(client: httpx.Client, org: str) -> set[str]:
    """Every dashboard title across all folders (best-effort; folder errors are ignored)."""
    titles: set[str] = set()
    try:
        folders = _folders(client, org)
    except Exception:  # noqa: BLE001 - fall back to the default folder only
        folders = [("default", "default")]
    seen: set[str] = set()
    for _name, fid in folders:
        if fid in seen:
            continue
        seen.add(fid)
        try:
            for d in _list_dashboards(client, org, fid):
                title = _dash_title(d)
                if title:
                    titles.add(title)
        except Exception:  # noqa: BLE001 - skip an unreadable folder
            continue
    return titles


def _expected_titles(root: Path) -> set[str]:
    """Titles declared by the dashboard JSON files under `root`."""
    titles: set[str] = set()
    for f in sorted(Path(root).glob("**/*.json")):
        try:
            title = json.loads(f.read_text()).get("title")
        except Exception:  # noqa: BLE001 - a malformed file simply contributes no title
            title = None
        if title:
            titles.add(title)
    return titles


@dashboards_app.command("list")
def dashboards_list(ctx: typer.Context):
    """List installed dashboards across folders (GET /api/{org}/dashboards)."""
    c = resolve(ctx.obj)
    rows: list[dict[str, str]] = []
    try:
        with c.client() as client:
            try:
                folders = _folders(client, c.org)
            except httpx.HTTPError:
                folders = [("default", "default")]
            for fname, fid in folders:
                for d in _list_dashboards(client, c.org, fid):
                    rows.append(
                        {
                            "title": _dash_title(d) or "",
                            "folder": fname,
                            "dashboardId": _dash_id(d) or "",
                        }
                    )
    except httpx.HTTPError as exc:
        _die(f"could not list dashboards: {exc}")
    _emit(c, rows, columns=["title", "folder", "dashboardId"], title="dashboards")


@dashboards_app.command("import")
def dashboards_import(
    ctx: typer.Context,
    path: Path = typer.Argument(None, help=f"dashboards dir or file (default: {DASHBOARDS_DIR})"),
):
    """Load dashboard JSON into OpenObserve, upserting by title (idempotent)."""
    c = resolve(ctx.obj)
    root = path or DASHBOARDS_DIR
    files_ = [root] if root.is_file() else sorted(root.glob("**/*.json"))
    if not files_:
        _die(f"no dashboard JSON found under {root}")

    rows: list[dict[str, str]] = []
    with c.client() as client:
        for f in files_:
            folder_name = "default" if f.parent == root else f.parent.name
            try:
                spec = json.loads(f.read_text())
            except Exception as exc:  # noqa: BLE001 - surface a bad file clearly
                _die(f"{f}: invalid JSON: {exc}")
            # Only dashboard objects (dicts with a title) are importable; skip anything else that
            # got swept up by the **/*.json glob (e.g. raw log-sample arrays), rather than
            # crashing on spec.get(). Mirrors the cloud-init provision script's title filter.
            if not isinstance(spec, dict) or not spec.get("title"):
                rows.append(
                    {"file": f.name, "folder": folder_name, "title": "", "action": "skipped"}
                )
                continue
            title = spec.get("title")
            try:
                fid = _ensure_folder(client, c.org, folder_name)
                # title -> (dashboardId, hash); hash is required by the PUT (update) API.
                existing = {
                    _dash_title(d): (
                        _dash_id(d),
                        d.get("hash") if isinstance(d, dict) else None,
                    )
                    for d in _list_dashboards(client, c.org, fid)
                }
                if title in existing and existing[title][0]:
                    did, dhash = existing[title]
                    params = {"folder": fid}
                    if dhash:
                        params["hash"] = dhash
                    resp = client.put(f"/api/{c.org}/dashboards/{did}", params=params, json=spec)
                    action = "updated"
                else:
                    resp = client.post(
                        f"/api/{c.org}/dashboards", params={"folder": fid}, json=spec
                    )
                    action = "created"
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                _die(f"import {f.name} failed: {exc}")
            rows.append(
                {"file": f.name, "folder": folder_name, "title": title or "", "action": action}
            )
    _emit(c, rows, columns=["file", "folder", "title", "action"], title="import")


@dashboards_app.command("delete")
def dashboards_delete(
    ctx: typer.Context,
    dashboard_id: str = typer.Argument(...),
    folder: str = typer.Option("default", "--folder"),
):
    """Delete a dashboard by id (DELETE /api/{org}/dashboards/<id>)."""
    c = resolve(ctx.obj)
    try:
        with c.client() as client:
            resp = client.delete(
                f"/api/{c.org}/dashboards/{dashboard_id}", params={"folder": folder}
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        _die(f"delete failed: {exc}")
    _emit(c, {"deleted": dashboard_id, "folder": folder}, title="delete")


# --- config -------------------------------------------------------------------

config_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Inspect config.")
app.add_typer(config_app, name="config")


@config_app.command("path")
def config_path():
    """Print the resolved default config file path."""
    console.print(str(ocfg.default_config_path()))


@config_app.command("list")
def config_list(ctx: typer.Context):
    """List profile names defined in the config file."""
    opts: Options = ctx.obj
    cfg_path = (
        opts.config or os.environ.get("OPENOBSERVECTL_CONFIG") or str(ocfg.default_config_path())
    )
    try:
        cfg = ocfg.load_config(cfg_path)
    except ocfg.ConfigError as exc:
        _die(str(exc))
    for name in sorted(cfg.profiles):
        console.print(name)


# --- check -------------------------------------------------------------------


@app.command()
def check(
    ctx: typer.Context,
    require_streams: bool = typer.Option(False, "--require-streams"),
    require_metrics: bool = typer.Option(
        False,
        "--require-metrics",
        help="fail unless PromQL `up` returns a series (metrics are being ingested)",
    ),
    require_logs: bool = typer.Option(
        False,
        "--require-logs",
        help="fail unless a logs stream has recent rows (logs are being ingested)",
    ),
    require_dashboards: bool = typer.Option(
        False,
        "--require-dashboards",
        help="fail unless every dashboard in --dashboards-dir is installed",
    ),
    dashboards_dir: Path = typer.Option(
        None,
        "--dashboards-dir",
        help=f"expected dashboards (default: {DASHBOARDS_DIR})",
    ),
):
    """Assert OpenObserve health + auth (+ optional streams/metrics/logs/dashboards); exit nonzero on failure."""
    c = resolve(ctx.obj)
    report = oc.CheckReport()

    # 1. Health (/healthz, no auth strictly required).
    try:
        with httpx.Client(base_url=c.base_url, timeout=c.timeout, verify=not c.insecure) as client:
            resp = client.get("/healthz")
            report.add("health", resp.status_code < 400, f"status={resp.status_code}")
    except httpx.HTTPError as exc:
        report.add("health", False, str(exc))
        _render_check(c, report)
        raise typer.Exit(report.exit_code) from None

    # 2. Auth works (streams endpoint requires basic auth).
    stream_count = None
    stream_items: list[dict[str, Any]] = []
    try:
        with c.client() as client:
            resp = client.get(f"/api/{c.org}/streams")
        if resp.status_code in (401, 403):
            report.add("auth", False, f"status={resp.status_code}")
        else:
            resp.raise_for_status()
            report.add("auth", True, f"status={resp.status_code}")
            data = resp.json()
            stream_items = data.get("list", []) if isinstance(data, dict) else (data or [])
            stream_count = len(stream_items)
    except httpx.HTTPError as exc:
        report.add("auth", False, str(exc))

    # 3. Streams present (reported always; only fails with --require-streams).
    if stream_count is None:
        report.skip("streams present", "streams not readable")
    elif require_streams:
        report.add("streams present", stream_count >= 1, f"{stream_count} streams")
    else:
        report.skip("streams present", f"{stream_count} streams (informational)")

    # 4. Metrics ingested — PromQL `up` returns at least one series (via remote_write).
    if require_metrics:
        try:
            with c.client() as client:
                resp = client.get(f"/api/{c.org}/prometheus/api/v1/query", params={"query": "up"})
                resp.raise_for_status()
                result = (resp.json().get("data") or {}).get("result") or []
            report.add("metrics present", len(result) >= 1, f"{len(result)} series for up")
        except httpx.HTTPError as exc:
            report.add("metrics present", False, str(exc))

    # 5. Logs ingested — a logs stream exists and has ≥1 recent row.
    if require_logs:
        logs_streams = [
            s
            for s in stream_items
            if isinstance(s, dict) and (s.get("stream_type") or s.get("type")) == "logs"
        ]
        if not logs_streams:
            report.add("logs present", False, "no logs streams")
        else:
            name = logs_streams[0].get("name")
            now_us = otail.now_micros()
            body = {
                "query": {
                    "sql": f'SELECT * FROM "{name}"',
                    "start_time": now_us - 3_600_000_000,
                    "end_time": now_us,
                    "size": 1,
                }
            }
            try:
                with c.client() as client:
                    resp = client.post(f"/api/{c.org}/_search", json=body)
                    resp.raise_for_status()
                    hits = resp.json().get("hits") or []
                report.add("logs present", len(hits) >= 1, f'{len(hits)} rows in "{name}"')
            except httpx.HTTPError as exc:
                report.add("logs present", False, str(exc))

    # 6. Dashboards installed — every expected dashboard title resolves.
    if require_dashboards:
        expected = _expected_titles(dashboards_dir or DASHBOARDS_DIR)
        if not expected:
            report.add("dashboards present", False, "no expected dashboards found")
        else:
            try:
                with c.client() as client:
                    installed = _installed_titles(client, c.org)
                missing = expected - installed
                report.add(
                    "dashboards present",
                    not missing,
                    f"{len(expected & installed)}/{len(expected)} present"
                    + (f", missing {sorted(missing)}" if missing else ""),
                )
            except httpx.HTTPError as exc:
                report.add("dashboards present", False, str(exc))

    _render_check(c, report)
    raise typer.Exit(report.exit_code)


def _render_check(c: Ctx, report: oc.CheckReport) -> None:
    if c.as_json:
        oc.print_json(report.to_dict())
        return
    table = Table("check", "status", "detail", title="openobserve check")
    colors = {"pass": "green", "fail": "red", "skip": "yellow"}
    for chk in report.checks:
        color = colors[chk.status]
        table.add_row(chk.name, f"[{color}]{chk.status}[/{color}]", chk.detail)
    console.print(table)
    verdict = "PASS" if report.passed else "FAIL"
    console.print(f"[{'green' if report.passed else 'red'}]{verdict}[/]")


if __name__ == "__main__":
    app()
