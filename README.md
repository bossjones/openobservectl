# openobservectl

CLI to verify, introspect, and import dashboards into an OpenObserve instance over its REST
API. OpenObserve ships no read SDK, so this talks to the REST API directly with
[`httpx`](https://www.python-httpx.org/). Built for scripted health checks (CI-friendly `check`
with a nonzero exit on failure), ad-hoc introspection (rich tables or `--json`), and idempotent
dashboard provisioning.

## Install

```bash
uv tool install openobservectl
# or, from a source checkout:
uv sync
```

## Resolving the server

`openobservectl` resolves the target OpenObserve URL (and credentials/org) in this order:

1. **Explicit flags** — `--server-url`, `--user`, `--password`, `--org`
2. **Environment** — `$OPENOBSERVE_URL`, `$OPENOBSERVE_USER`, `$OPENOBSERVE_PASSWORD`
3. **A profile** in `~/.openobservectl/config.yaml` (`--profile <name>` or `$OPENOBSERVECTL_PROFILE`;
   if neither is given but the config file exists, its `default` profile is used automatically)
4. **`tofu output`** — for a [multipass-lab](https://github.com/bossjones/multipass-lab)-style
   checkout, pass `--lab-root <path>` (or `$MULTIPASS_LAB_ROOT`) and the `--cluster` name; the
   server IP is resolved by running OpenTofu in that checkout. This is a fallback for that one
   deployment pattern, not a requirement — most users will use a profile or `$OPENOBSERVE_URL`.

If none of the above resolves a URL, the CLI exits nonzero with a message listing the options.

### `~/.openobservectl/config.yaml`

```yaml
profiles:
  default:
    endpoint: https://openobserve.example.com
    organization: default
    username: user@example.com
    password: your-password
  staging:
    endpoint: https://openobserve-staging.example.com
    organization: default
    username: user@example.com
    password: your-password
    timeout: 30
    verify: false
```

`openobservectl config path` prints the resolved config path; `openobservectl config list`
prints the configured profile names.

## Commands

| Command | Endpoint | Purpose |
|---|---|---|
| `health` | `GET /healthz` | liveness probe (no auth) |
| `streams [--type logs\|metrics\|traces]` | `GET /api/{org}/streams` | list ingest streams |
| `search SQL [--start --end --size]` | `POST /api/{org}/_search` | SQL search over a stream |
| `query PROMQL` | `GET /api/{org}/prometheus/api/v1/query` | PromQL-compatible metrics query |
| `orgs` | `GET /api/organizations` | list organizations |
| `dashboards list` | `GET /api/{org}/dashboards` | list installed dashboards across folders |
| `dashboards import [PATH]` | `POST`/`PUT /api/{org}/dashboards` | upsert dashboard JSON by title (idempotent); defaults to the vendored dashboards |
| `dashboards delete DASHBOARD_ID [--folder]` | `DELETE /api/{org}/dashboards/<id>` | delete a dashboard |
| `check [--require-streams] [--require-metrics] [--require-logs] [--require-dashboards]` | composite | assert health/auth (+ optional streams/metrics/logs/dashboards); exits 0 pass / 2 fail |

Global options: `--server-url`, `--user`, `--password`, `--org` (default `default`), `--json`,
`--timeout`, `--insecure`, `--profile`/`-p`, `--config`, `--cluster`, `--lab-root`.

Twelve dashboards ship as package data (`Correlation/`, `Infrastructure/`, `LogAnalysis/`) and
are what `dashboards import` and `check --require-dashboards` use by default.

## Development

```bash
uv sync
just check   # ruff format --check + ruff check + basedpyright + codespell + pytest
just test    # pytest only
```

See [`docs/design.md`](docs/design.md) for the original design doc (predates the `dashboards`
subcommand and the config-profile layer — see the precedence section above for current behavior).
