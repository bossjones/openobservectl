"""Hermetic behavior tests for the `dashboards` sub-typer + `check --require-dashboards`,
plus a validity sweep over the shipped (vendored) dashboard JSON.

Drives the CLI via typer's CliRunner against a pytest-httpserver serving canned
OpenObserve folder/dashboard responses. `--server-url` avoids any `tofu` invocation.
"""

import base64
import json
from importlib.resources import files
from pathlib import Path

from typer.testing import CliRunner

from openobservectl import cli as oo

runner = CliRunner()

DEFAULT_AUTH = "Basic " + base64.b64encode(b"admin@example.com:Complexpass#123").decode()

SHIPPED_DIR = Path(str(files("openobservectl") / "dashboards"))
EXPECTED_TITLES = {
    "Log Overview",
    "Error Triage",
    "Per-Host / Per-Stream",
    "Container Logs",
    "Kubernetes Pod Logs",
    "Cause & Effect",
    "Prometheus Health",
    "Uptime",
    "Traces Overview",
    "Traces By Service",
    "Host Metrics",
    "Container Metrics",
}


def _run(base, *args):
    return runner.invoke(oo.app, ["--server-url", base, *args])


def _write_dash(path: Path, title: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 5, "title": title, "tabs": [{"panels": []}]}))


def _base_check_server(httpserver, *, dashboards=None):
    """Endpoints the `check` command hits before the dashboards assertion."""
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/api/default/streams", method="GET").respond_with_json(
        {"list": [{"name": "host_logs", "stream_type": "logs"}]}
    )
    httpserver.expect_request("/api/v2/default/folders/dashboards", method="GET").respond_with_json(
        {"list": []}
    )
    httpserver.expect_request("/api/default/dashboards", method="GET").respond_with_json(
        {"dashboards": dashboards or []}
    )
    return httpserver.url_for("")


def _panels(dash):
    """Collect panels whether at top level or nested under tabs (v5)."""
    panels = list(dash.get("panels") or [])
    for tab in dash.get("tabs") or []:
        panels.extend(tab.get("panels") or [])
    return panels


# ---------------------------------------------------------------- dashboards list


def test_dashboards_list_reads_folders_and_dashboards(httpserver):
    httpserver.expect_request("/api/v2/default/folders/dashboards", method="GET").respond_with_json(
        {"list": [{"folderId": "Logs", "name": "Logs"}]}
    )
    httpserver.expect_request("/api/default/dashboards", method="GET").respond_with_json(
        {"dashboards": [{"dashboardId": "d1", "title": "Log Overview"}]}
    )
    r = _run(httpserver.url_for(""), "--json", "dashboards", "list")
    assert r.exit_code == 0, r.output
    titles = {row["title"] for row in json.loads(r.output)}
    assert "Log Overview" in titles


def test_dashboards_list_sends_basic_auth(httpserver):
    httpserver.expect_request(
        "/api/v2/default/folders/dashboards",
        method="GET",
        headers={"Authorization": DEFAULT_AUTH},
    ).respond_with_json({"list": []})
    httpserver.expect_request(
        "/api/default/dashboards", method="GET", headers={"Authorization": DEFAULT_AUTH}
    ).respond_with_json({"dashboards": []})
    r = _run(httpserver.url_for(""), "--json", "dashboards", "list")
    assert r.exit_code == 0, r.output


# -------------------------------------------------------------- dashboards import


def test_import_creates_folder_and_posts(httpserver, tmp_path):
    _write_dash(tmp_path / "Logs" / "board.json", "Log Overview")
    httpserver.expect_request("/api/v2/default/folders/dashboards", method="GET").respond_with_json(
        {"list": []}
    )
    httpserver.expect_request(
        "/api/v2/default/folders/dashboards", method="POST"
    ).respond_with_json({"folderId": "Logs", "name": "Logs"})
    httpserver.expect_request("/api/default/dashboards", method="GET").respond_with_json(
        {"dashboards": []}
    )
    httpserver.expect_request("/api/default/dashboards", method="POST").respond_with_json(
        {"dashboardId": "new"}
    )
    r = _run(httpserver.url_for(""), "--json", "dashboards", "import", str(tmp_path))
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)[0]["action"] == "created"
    methods = {(req.method, req.path) for req, _ in httpserver.log}
    assert ("POST", "/api/v2/default/folders/dashboards") in methods
    assert ("POST", "/api/default/dashboards") in methods


def test_import_skips_non_dashboard_json(httpserver, tmp_path):
    """A top-level JSON array (e.g. raw log-sample files) is swept by the **/*.json
    glob but must be skipped, not crash the importer with .get() on a list."""
    _write_dash(tmp_path / "board.json", "Log Overview")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "samples.json").write_text(json.dumps([{"event": "x"}]))
    httpserver.expect_request("/api/default/dashboards", method="GET").respond_with_json(
        {"dashboards": []}
    )
    httpserver.expect_request("/api/default/dashboards", method="POST").respond_with_json(
        {"dashboardId": "new"}
    )
    r = _run(httpserver.url_for(""), "--json", "dashboards", "import", str(tmp_path))
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    actions = {row["file"]: row["action"] for row in rows}
    assert actions["board.json"] == "created"
    assert actions["samples.json"] == "skipped"


def test_import_sends_basic_auth_on_post(httpserver, tmp_path):
    _write_dash(tmp_path / "board.json", "Log Overview")
    httpserver.expect_request("/api/default/dashboards", method="GET").respond_with_json(
        {"dashboards": []}
    )
    httpserver.expect_request(
        "/api/default/dashboards",
        method="POST",
        headers={"Authorization": DEFAULT_AUTH},
    ).respond_with_json({"dashboardId": "new"})
    r = _run(httpserver.url_for(""), "--json", "dashboards", "import", str(tmp_path))
    assert r.exit_code == 0, r.output


def test_import_upserts_by_title(httpserver, tmp_path):
    """When a dashboard with the same title exists, import PUTs it (no second create)."""
    _write_dash(tmp_path / "board.json", "Log Overview")
    httpserver.expect_request("/api/default/dashboards", method="GET").respond_with_json(
        {"dashboards": [{"dashboard_id": "abc", "title": "Log Overview", "hash": "h1"}]}
    )
    httpserver.expect_request(
        "/api/default/dashboards/abc",
        method="PUT",
        query_string={"folder": "default", "hash": "h1"},
    ).respond_with_json({"dashboardId": "abc"})
    r = _run(httpserver.url_for(""), "--json", "dashboards", "import", str(tmp_path))
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)[0]["action"] == "updated"
    methods = {(req.method, req.path) for req, _ in httpserver.log}
    assert ("PUT", "/api/default/dashboards/abc") in methods
    assert ("POST", "/api/default/dashboards") not in methods


# -------------------------------------------------------------- dashboards delete


def test_delete_sends_delete_request(httpserver):
    httpserver.expect_request("/api/default/dashboards/xyz", method="DELETE").respond_with_json(
        {"code": 200}
    )
    r = _run(httpserver.url_for(""), "--json", "dashboards", "delete", "xyz")
    assert r.exit_code == 0, r.output
    methods = {(req.method, req.path) for req, _ in httpserver.log}
    assert ("DELETE", "/api/default/dashboards/xyz") in methods


# ----------------------------------------------------- check --require-dashboards


def test_check_require_dashboards_passes_when_present(httpserver, tmp_path):
    _write_dash(tmp_path / "board.json", "Log Overview")
    base = _base_check_server(
        httpserver, dashboards=[{"dashboardId": "d1", "title": "Log Overview"}]
    )
    r = _run(base, "--json", "check", "--require-dashboards", "--dashboards-dir", str(tmp_path))
    assert r.exit_code == 0, r.output
    checks = json.loads(r.output)["checks"]
    assert any(c["name"] == "dashboards present" and c["status"] == "pass" for c in checks)


def test_check_require_dashboards_fails_when_missing(httpserver, tmp_path):
    _write_dash(tmp_path / "board.json", "Log Overview")
    base = _base_check_server(httpserver, dashboards=[])
    r = _run(base, "--json", "check", "--require-dashboards", "--dashboards-dir", str(tmp_path))
    assert r.exit_code == 2
    checks = json.loads(r.output)["checks"]
    assert any(c["name"] == "dashboards present" and c["status"] == "fail" for c in checks)


# ------------------------------------------------------- shipped dashboard JSON


def test_shipped_dashboards_are_valid_json_with_title_and_panels():
    # Defensive: exclude any `logs/` dir even though the vendor copy shouldn't include one.
    files_ = sorted(f for f in SHIPPED_DIR.glob("**/*.json") if "logs" not in f.parts)
    assert files_, f"no dashboard JSON found under {SHIPPED_DIR}"
    titles = set()
    for f in files_:
        dash = json.loads(f.read_text())  # raises on invalid JSON
        assert dash.get("title"), f"{f} missing title"
        assert _panels(dash), f"{f} has no panels"
        titles.add(dash["title"])
    assert EXPECTED_TITLES <= titles, f"missing dashboards: {EXPECTED_TITLES - titles}"


def test_shipped_dashboards_count_is_twelve():
    files_ = list(SHIPPED_DIR.glob("**/*.json"))
    assert len(files_) == 12
