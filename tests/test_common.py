"""Hermetic unit tests for openobservectl.common (TDD).

These never call `tofu` or touch a network except a throwaway in-process HTTP
server (pytest-httpserver). Target resolution injects a fake `runner` instead of
shelling out to OpenTofu.
"""

import base64
import json

import pytest

from openobservectl import common as oc


def _basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


# --------------------------------------------------------------- parse_tofu_output


def test_parse_tofu_output_extracts_ip_and_flags():
    data = {
        "server_ipv4": {"value": "10.0.0.5"},
        "enabled_exporters": {"value": ["enable_node_exporter", "enable_openobserve"]},
    }
    ip, flags = oc.parse_tofu_output(data)
    assert ip == "10.0.0.5"
    assert flags == {"enable_node_exporter", "enable_openobserve"}


def test_parse_tofu_output_missing_flags_defaults_empty():
    ip, flags = oc.parse_tofu_output({"server_ipv4": {"value": "1.2.3.4"}})
    assert ip == "1.2.3.4"
    assert flags == set()


# ------------------------------------------------------------------- default_chdir


def test_default_chdir_joins_lab_root_and_cluster():
    chdir = oc.default_chdir("centralized_logging", "/some/lab")
    assert chdir.endswith("clusters/centralized_logging")
    assert chdir.startswith("/some/lab")


def test_default_chdir_expands_user():
    chdir = oc.default_chdir("centralized_monitoring", "~/lab")
    assert "~" not in chdir
    assert chdir.endswith("clusters/centralized_monitoring")


# ----------------------------------------------------------------- resolve_target


def test_resolve_target_prefers_server_url_and_skips_tofu():
    calls = []
    t = oc.resolve_target(
        port=3000, server_url="http://host:3000/", runner=lambda c: calls.append(c)
    )
    assert t.base_url == "http://host:3000"  # trailing slash stripped
    assert t.ip is None
    assert t.enabled_flags == set()
    assert calls == []  # tofu never invoked when a URL is given


def test_resolve_target_uses_env_url_when_no_flag():
    t = oc.resolve_target(
        port=3000,
        url_env="GRAFANA_URL",
        env={"GRAFANA_URL": "http://x:3000"},
        runner=lambda c: pytest.fail("tofu should not run"),
    )
    assert t.base_url == "http://x:3000"


def test_resolve_target_explicit_url_beats_env():
    t = oc.resolve_target(
        port=3000,
        server_url="http://a:3000",
        url_env="GRAFANA_URL",
        env={"GRAFANA_URL": "http://b:3000"},
        runner=lambda c: {},
    )
    assert t.base_url == "http://a:3000"


def test_resolve_target_via_tofu_builds_url_and_flags():
    data = {
        "server_ipv4": {"value": "10.9.8.7"},
        "enabled_exporters": {"value": ["enable_openobserve"]},
    }
    t = oc.resolve_target(
        port=5080,
        chdir=oc.default_chdir("centralized_monitoring", "/fake/lab"),
        runner=lambda chdir: data,
    )
    assert t.base_url == "http://10.9.8.7:5080"
    assert t.ip == "10.9.8.7"
    assert t.enabled_flags == {"enable_openobserve"}


def test_resolve_target_passes_chdir_to_runner():
    seen = {}

    def runner(chdir):
        seen["chdir"] = chdir
        return {"server_ipv4": {"value": "1.1.1.1"}}

    oc.resolve_target(
        port=9090, chdir=oc.default_chdir("centralized_logging", "/fake/lab"), runner=runner
    )
    assert seen["chdir"].endswith("clusters/centralized_logging")


def test_resolve_target_without_chdir_raises():
    with pytest.raises(ValueError, match="chdir"):
        oc.resolve_target(port=9090, runner=lambda c: pytest.fail("tofu should not run"))


# ------------------------------------------------------------ resolve_credentials


def test_resolve_credentials_explicit_wins():
    assert oc.resolve_credentials("u", "p", default_user="admin", default_password="admin") == (
        "u",
        "p",
    )


def test_resolve_credentials_env_fallback():
    assert oc.resolve_credentials(
        None,
        None,
        user_env="GU",
        pass_env="GP",
        default_user="admin",
        default_password="admin",
        env={"GU": "eu", "GP": "ep"},
    ) == ("eu", "ep")


def test_resolve_credentials_default_fallback():
    assert oc.resolve_credentials(
        None, None, default_user="admin", default_password="secret", env={}
    ) == ("admin", "secret")


# -------------------------------------------------------------------- CheckReport


def test_check_report_pass_and_skip_exit_zero():
    r = oc.CheckReport()
    r.add("health", True, "ok")
    r.skip("openobserve", "flag off")
    assert r.passed is True
    assert r.exit_code == 0
    d = r.to_dict()
    assert d["ok"] is True
    assert d["checks"][0] == {"name": "health", "status": "pass", "detail": "ok"}
    assert d["checks"][1]["status"] == "skip"


def test_check_report_any_fail_exit_two():
    r = oc.CheckReport()
    r.add("health", True)
    r.add("datasource", False, "unhealthy")
    assert r.passed is False
    assert r.exit_code == 2


# --------------------------------------------------------------------------- poll


def test_poll_returns_first_truthy():
    seq = iter([None, 0, "yes"])
    assert oc.poll(lambda: next(seq), timeout=5, interval=0) == "yes"


def test_poll_returns_last_falsy_on_timeout():
    assert oc.poll(lambda: None, timeout=0, interval=0) is None


def test_poll_catches_listed_exceptions():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ValueError("boom")
        return "ok"

    assert oc.poll(flaky, timeout=5, interval=0, catch=(ValueError,)) == "ok"


# ------------------------------------------------------------------ http_get_json


def test_http_get_json_parses_and_sends_basic_auth(httpserver):
    httpserver.expect_request(
        "/api/data", headers={"Authorization": _basic("u", "p")}
    ).respond_with_json({"ok": True})
    out = oc.http_get_json(httpserver.url_for("/api/data"), auth=("u", "p"))
    assert out == {"ok": True}


def test_http_get_json_raises_httperror_on_500(httpserver):
    httpserver.expect_request("/bad").respond_with_data("nope", status=500)
    with pytest.raises(oc.HttpError) as excinfo:
        oc.http_get_json(httpserver.url_for("/bad"))
    assert excinfo.value.status == 500


def test_http_get_json_raises_on_connection_refused():
    # Nothing is listening on this port.
    with pytest.raises(oc.HttpError):
        oc.http_get_json("http://127.0.0.1:1/nope", timeout=1)


# ---------------------------------------------------------------------- print_json


def test_print_json_emits_parseable_json(capsys):
    oc.print_json({"a": 1, "b": [1, 2]})
    out = capsys.readouterr().out
    assert json.loads(out) == {"a": 1, "b": [1, 2]}
