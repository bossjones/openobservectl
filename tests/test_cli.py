"""Hermetic behavior tests for openobservectl.cli (TDD).

Drives the CLI via typer's CliRunner against a pytest-httpserver serving canned
OpenObserve REST responses. `--server-url` avoids any `tofu` invocation.
"""

import base64
import json

from typer.testing import CliRunner

from openobservectl import cli as oo

runner = CliRunner()

DEFAULT_AUTH = "Basic " + base64.b64encode(b"admin@example.com:Complexpass#123").decode()


def _server(httpserver, *, streams=None):
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    st = streams if streams is not None else [{"name": "default", "stream_type": "logs"}]
    httpserver.expect_request("/api/default/streams").respond_with_json({"list": st})
    return httpserver.url_for("")


def _run(base, *args):
    return runner.invoke(oo.app, ["--server-url", base, *args])


# ------------------------------------------------------------------ introspection


def test_health_reports_status(httpserver):
    base = _server(httpserver)
    r = _run(base, "--json", "health")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["status"] == "ok"


def test_streams_lists_names(httpserver):
    base = _server(httpserver, streams=[{"name": "traces", "stream_type": "traces"}])
    r = _run(base, "--json", "streams")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)[0]["name"] == "traces"


def test_streams_sends_basic_auth(httpserver):
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request(
        "/api/default/streams", headers={"Authorization": DEFAULT_AUTH}
    ).respond_with_json({"list": [{"name": "default", "stream_type": "logs"}]})
    r = _run(httpserver.url_for(""), "--json", "streams")
    assert r.exit_code == 0, r.output


def test_streams_surfaces_error_body_when_response_has_no_list_key(httpserver):
    # A 200 response without a "list" key (e.g. an auth/permission error body) must stay
    # visible to the user, not silently collapse into an empty streams table.
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/api/default/streams").respond_with_json(
        {"code": 403, "message": "permission denied"}
    )
    r = _run(httpserver.url_for(""), "--json", "streams")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {"code": 403, "message": "permission denied"}


def test_search_returns_hits(httpserver):
    base = _server(httpserver)
    httpserver.expect_request("/api/default/_search", method="POST").respond_with_json(
        {"hits": [{"_timestamp": 1, "log": "hello"}], "total": 1}
    )
    r = _run(base, "--json", "search", "SELECT * FROM default")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)[0]["log"] == "hello"


def test_query_promql(httpserver):
    base = _server(httpserver)
    httpserver.expect_request("/api/default/prometheus/api/v1/query").respond_with_json(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {}, "value": [1, "1"]}],
            },
        }
    )
    r = _run(base, "--json", "query", "up")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["data"]["result"][0]["value"][1] == "1"


# ------------------------------------------------------------------------ logs tail


def test_logs_tail_without_follow_single_window(httpserver):
    base = _server(httpserver)
    httpserver.expect_request("/api/default/_search", method="POST").respond_with_json(
        {"hits": [{"_timestamp": 2_000_000_000_000_000, "log": "hello world"}]}
    )
    r = _run(base, "logs", "tail", "--stream", "default", "--since", "5m")
    assert r.exit_code == 0, r.output
    assert "hello world" in r.output


def test_logs_tail_autodiscovers_logs_streams(httpserver):
    base = _server(httpserver, streams=[{"name": "container_logs", "stream_type": "logs"}])
    httpserver.expect_request("/api/default/_search", method="POST").respond_with_json(
        {"hits": [{"_timestamp": 2_000_000_000_000_000, "log": "hello world"}]}
    )
    r = _run(base, "logs", "tail", "--since", "5m")
    assert r.exit_code == 0, r.output
    assert "container_logs" in r.output  # status line
    assert "hello world" in r.output


def test_logs_tail_no_logs_streams_exits_nonzero(httpserver):
    base = _server(httpserver, streams=[])
    r = _run(base, "logs", "tail", "--since", "5m")
    assert r.exit_code != 0
    assert "no logs streams" in r.output


def test_logs_tail_stream_and_sql_together_exits_nonzero(httpserver):
    # Combining --stream with --sql would otherwise run the identical --sql query
    # once per --stream, independently, duplicating every emitted hit.
    base = _server(httpserver)
    r = _run(base, "logs", "tail", "--stream", "a", "--sql", "SELECT * FROM x", "--since", "5m")
    assert r.exit_code != 0
    assert "--stream" in r.output and "--sql" in r.output


def test_logs_tail_sql_override_skips_autodiscovery(httpserver):
    base = _server(httpserver, streams=[])  # streams() must NOT be needed/called
    httpserver.expect_request("/api/default/_search", method="POST").respond_with_json(
        {"hits": [{"_timestamp": 2_000_000_000_000_000, "log": "custom"}]}
    )
    r = _run(base, "logs", "tail", "--sql", "SELECT * FROM x", "--since", "5m")
    assert r.exit_code == 0, r.output
    assert "custom" in r.output


def test_logs_tail_json_flag_emits_compact_json_lines_only(httpserver):
    base = _server(httpserver, streams=[{"name": "container_logs", "stream_type": "logs"}])
    httpserver.expect_request("/api/default/_search", method="POST").respond_with_json(
        {"hits": [{"_timestamp": 2_000_000_000_000_000, "log": "hello"}]}
    )
    r = _run(base, "--json", "logs", "tail", "--since", "5m")
    assert r.exit_code == 0, r.output
    assert "tailing:" not in r.output  # --json output stays clean/jq-able (deviation from ooctl)
    line = r.output.strip().splitlines()[-1]
    assert json.loads(line)["log"] == "hello"


def test_logs_tail_invalid_since_exits_nonzero(httpserver):
    base = _server(httpserver)
    r = _run(base, "logs", "tail", "--since", "bogus")
    assert r.exit_code != 0
    assert "invalid" in r.output.lower()


# -------------------------------------------------------------------------- check


def test_check_passes_when_healthy_and_authed(httpserver):
    base = _server(httpserver)
    r = _run(base, "--json", "check")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["ok"] is True


def test_check_fails_on_auth_401(httpserver):
    httpserver.expect_request("/healthz").respond_with_json({"status": "ok"})
    httpserver.expect_request("/api/default/streams").respond_with_data("unauthorized", status=401)
    r = _run(httpserver.url_for(""), "--json", "check")
    assert r.exit_code == 2
    checks = json.loads(r.output)["checks"]
    assert any(c["name"] == "auth" and c["status"] == "fail" for c in checks)


def test_check_streams_present_is_skip_by_default_when_empty(httpserver):
    base = _server(httpserver, streams=[])
    r = _run(base, "--json", "check")
    assert r.exit_code == 0, r.output


def test_check_require_streams_fails_when_empty(httpserver):
    base = _server(httpserver, streams=[])
    r = _run(base, "--json", "check", "--require-streams")
    assert r.exit_code == 2
    checks = json.loads(r.output)["checks"]
    assert any(c["name"] == "streams present" and c["status"] == "fail" for c in checks)


def test_check_fails_on_connection_refused():
    r = runner.invoke(oo.app, ["--server-url", "http://127.0.0.1:1", "--json", "check"])
    assert r.exit_code == 2


# ---------------------------------------------------- check --require-metrics/logs


def test_check_require_metrics_passes_when_up_returns_series(httpserver):
    base = _server(httpserver)
    httpserver.expect_request("/api/default/prometheus/api/v1/query").respond_with_json(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"__name__": "up"}, "value": [1, "1"]}],
            },
        }
    )
    r = _run(base, "--json", "check", "--require-metrics")
    assert r.exit_code == 0, r.output
    checks = json.loads(r.output)["checks"]
    assert any(c["name"] == "metrics present" and c["status"] == "pass" for c in checks)


def test_check_require_metrics_fails_when_no_series(httpserver):
    base = _server(httpserver)
    httpserver.expect_request("/api/default/prometheus/api/v1/query").respond_with_json(
        {"status": "success", "data": {"resultType": "vector", "result": []}}
    )
    r = _run(base, "--json", "check", "--require-metrics")
    assert r.exit_code == 2
    checks = json.loads(r.output)["checks"]
    assert any(c["name"] == "metrics present" and c["status"] == "fail" for c in checks)


def test_check_require_logs_passes_when_stream_has_rows(httpserver):
    base = _server(httpserver, streams=[{"name": "container_logs", "stream_type": "logs"}])
    httpserver.expect_request("/api/default/_search", method="POST").respond_with_json(
        {"hits": [{"_timestamp": 1, "body": "hello"}], "total": 1}
    )
    r = _run(base, "--json", "check", "--require-logs")
    assert r.exit_code == 0, r.output
    checks = json.loads(r.output)["checks"]
    assert any(c["name"] == "logs present" and c["status"] == "pass" for c in checks)


def test_check_require_logs_fails_when_no_logs_streams(httpserver):
    base = _server(httpserver, streams=[{"name": "up", "stream_type": "metrics"}])
    r = _run(base, "--json", "check", "--require-logs")
    assert r.exit_code == 2
    checks = json.loads(r.output)["checks"]
    assert any(c["name"] == "logs present" and c["status"] == "fail" for c in checks)


# ------------------------------------------------------------- URL/cred precedence


def test_server_url_flag_beats_env_and_profile(httpserver, tmp_path, monkeypatch):
    base = _server(httpserver)
    monkeypatch.setenv("OPENOBSERVE_URL", "http://127.0.0.1:1")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n  default:\n    endpoint: http://127.0.0.1:2\n"
        "    username: u\n    password: p\n"
    )
    monkeypatch.setenv("OPENOBSERVECTL_CONFIG", str(cfg))
    r = runner.invoke(oo.app, ["--server-url", base, "--json", "health"])
    assert r.exit_code == 0, r.output


def test_env_url_beats_profile(tmp_path, monkeypatch, httpserver):
    base = _server(httpserver)
    monkeypatch.setenv("OPENOBSERVE_URL", base)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "profiles:\n  default:\n    endpoint: http://127.0.0.1:1\n"
        "    username: u\n    password: p\n"
    )
    monkeypatch.setenv("OPENOBSERVECTL_CONFIG", str(cfg))
    r = runner.invoke(oo.app, ["--json", "health"])
    assert r.exit_code == 0, r.output


def test_profile_endpoint_used_when_no_flag_or_env(tmp_path, monkeypatch, httpserver):
    base = _server(httpserver)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"profiles:\n  default:\n    endpoint: {base}\n"
        "    username: admin@example.com\n    password: 'Complexpass#123'\n"
    )
    monkeypatch.delenv("OPENOBSERVE_URL", raising=False)
    monkeypatch.setenv("OPENOBSERVECTL_CONFIG", str(cfg))
    r = runner.invoke(oo.app, ["--json", "health"])
    assert r.exit_code == 0, r.output


def test_no_source_exits_nonzero_with_guidance(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENOBSERVE_URL", raising=False)
    monkeypatch.delenv("MULTIPASS_LAB_ROOT", raising=False)
    monkeypatch.setenv("OPENOBSERVECTL_CONFIG", str(tmp_path / "nope.yaml"))
    r = runner.invoke(oo.app, ["health"])
    assert r.exit_code != 0
    assert "server-url" in r.output or "OPENOBSERVE_URL" in r.output
