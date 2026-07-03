"""Shared helpers for openobservectl (stdlib-only).

Responsibilities:
  * resolve the live server base URL from `tofu output -json` (or an override),
  * resolve credentials with a flag > env > default precedence,
  * a stdlib JSON HTTP GET (for endpoints the client libs don't wrap),
  * a `poll()` readiness helper,
  * a `CheckReport` accumulator that renders to a dict + drives the `check` exit code.

Deliberately depends on nothing outside the standard library — rich/typer rendering
lives in `cli.py`.
"""

from __future__ import annotations

import base64
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Check exit code (nonzero, distinct from argparse/typer's usage exit 2 collisions are
# acceptable — any nonzero means "verification failed").
CHECK_FAIL_EXIT = 2


# --- OpenTofu output resolution ----------------------------------------------


def run_tofu_output(chdir: str) -> dict[str, Any]:
    """Return the parsed `tofu -chdir=<chdir> output -json` document."""
    raw = subprocess.run(
        ["tofu", f"-chdir={chdir}", "output", "-json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(raw)


def parse_tofu_output(tofu_json: dict[str, Any]) -> tuple[str, set[str]]:
    """Extract (server_ipv4, enabled_flags) from a parsed `tofu output -json`."""
    ip = tofu_json["server_ipv4"]["value"]
    flags = set(tofu_json.get("enabled_exporters", {}).get("value", []))
    return ip, flags


def default_chdir(cluster: str, lab_root: str | Path) -> str:
    """The cluster dir `tofu` should run in, under an explicit multipass-lab checkout."""
    return str(Path(lab_root).expanduser() / "clusters" / cluster)


@dataclass
class Target:
    """A resolved service endpoint plus the cluster's enabled feature flags."""

    base_url: str
    ip: str | None = None
    enabled_flags: set[str] = field(default_factory=set)


def resolve_target(
    *,
    port: int,
    server_url: str | None = None,
    url_env: str | None = None,
    chdir: str | None = None,
    env: Mapping[str, str] | None = None,
    runner: Callable[[str], dict[str, Any]] = run_tofu_output,
) -> Target:
    """Resolve a service base URL.

    Precedence: explicit ``server_url`` > ``$url_env`` > `tofu output`
    (``http://<ip>:<port>``, resolved via ``runner(chdir)``). When a URL override is
    used, `tofu` is never invoked and ``enabled_flags`` is empty. The tofu path
    requires an explicit ``chdir`` (see :func:`default_chdir`) — there is no implicit
    cluster/lab-root resolution here.
    """
    import os

    env = os.environ if env is None else env
    if server_url is None and url_env:
        server_url = env.get(url_env)

    if server_url:
        return Target(base_url=server_url.rstrip("/"))

    if chdir is None:
        raise ValueError(
            "chdir is required to resolve via `tofu output` "
            "(no server_url/url_env was set; pass chdir=default_chdir(cluster, lab_root))"
        )

    ip, flags = parse_tofu_output(runner(chdir))
    return Target(base_url=f"http://{ip}:{port}", ip=ip, enabled_flags=flags)


def resolve_credentials(
    user: str | None,
    password: str | None,
    *,
    user_env: str | None = None,
    pass_env: str | None = None,
    default_user: str,
    default_password: str,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Resolve (user, password) with flag > env > default precedence."""
    import os

    env = os.environ if env is None else env
    if user is None and user_env:
        user = env.get(user_env)
    if password is None and pass_env:
        password = env.get(pass_env)
    return (
        user if user is not None else default_user,
        password if password is not None else default_password,
    )


# --- stdlib HTTP -------------------------------------------------------------


class HttpError(Exception):
    """A non-2xx response or a transport failure. ``status`` is 0 for transport errors."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(f"HTTP {status}: {message}" if status else f"connection error: {message}")
        self.status = status


def http_get_json(
    url: str,
    *,
    auth: tuple[str, str] | None = None,
    timeout: float = 10.0,
    headers: Mapping[str, str] | None = None,
    insecure: bool = False,
) -> Any:
    """GET ``url`` and parse JSON. Raises :class:`HttpError` on non-2xx / transport error."""
    req = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    ctx = ssl._create_unverified_context() if insecure else None  # pyright: ignore[reportPrivateUsage]
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, exc.reason) from exc
    except urllib.error.URLError as exc:
        raise HttpError(0, str(exc.reason)) from exc
    return json.loads(body) if body else None


# --- readiness polling -------------------------------------------------------


def poll(
    fn: Callable[[], object],
    *,
    timeout: float = 60.0,
    interval: float = 2.0,
    catch: tuple[type[BaseException], ...] = (),
):
    """Call ``fn`` until it returns a truthy value or ``timeout`` elapses.

    Returns the first truthy result, or the last (falsy) result on timeout. Exceptions
    listed in ``catch`` are treated as a falsy attempt.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            res = fn()
        except catch:
            res = None
        if res:
            return res
        if time.monotonic() >= deadline:
            return res
        time.sleep(interval)


# --- check reporting ---------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str  # "pass" | "fail" | "skip"
    detail: str = ""


class CheckReport:
    """Accumulates individual assertions and derives an overall pass/fail + exit code."""

    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, "pass" if ok else "fail", detail))

    def skip(self, name: str, detail: str = "") -> None:
        self.checks.append(Check(name, "skip", detail))

    @property
    def passed(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else CHECK_FAIL_EXIT

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.passed,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks
            ],
        }


# --- output ------------------------------------------------------------------


def print_json(obj: Any) -> None:
    """Emit clean, parseable JSON to stdout (for `--json` / piping)."""
    print(json.dumps(obj, indent=2, default=str))
