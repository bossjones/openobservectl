# Pyrefly type checking

Spec for adopting [**Pyrefly**](https://pyrefly.org/) — Meta's fast, Rust-based Python type
checker and language server — in `openobservectl`, **alongside** the existing `basedpyright`
setup rather than replacing it.

## Why Pyrefly, and why alongside basedpyright

`openobservectl` already type-checks `src/` with `basedpyright` (0 errors today) inside `just lint`
/ `just check` and CI. Pyrefly is added as a **parallel, non-blocking lane** so we can:

- Evaluate a second, much faster checker (Rust; v1.1 is ~27% faster than v1.0) without risk to the
  green build.
- Drive a **typing-error feedback loop** (baseline burn-down + coverage delta) instead of a single
  pass/fail gate.
- Exercise Pyrefly's **built-in Pydantic v2 support** — directly relevant to
  `src/openobservectl/config.py`, which uses `pydantic.BaseModel` / `Field` / `field_validator`.

This "run more than one checker" posture is endorsed by Pyrefly itself (see
[*Are you really expected to run five type-checkers now?*](https://pyrefly.org/blog/too-many-type-checkers/)):
prioritise running as many checkers as possible on the **test suite**, at least one on source — which
is why this spec checks both `src` and `tests`.

## Status

- Pyrefly latest: **v1.1** (released 2026-06-17). Pydantic support requires **>= 0.33.0**; bundled
  typeshed stubs since **0.40**; bundled popular-library stubs since **0.46.0**. Any current release
  satisfies all of these.

## Important URLs

### Documentation
| Topic | URL |
|---|---|
| Homepage | https://pyrefly.org/ |
| Docs intro | https://pyrefly.org/en/docs/ |
| Installation | https://pyrefly.org/en/docs/installation/ |
| Typing for Python Developers | https://pyrefly.org/en/docs/typing-for-python-developers/ |
| Import Resolution | https://pyrefly.org/en/docs/import-resolution/ |
| IDE Installation | https://pyrefly.org/en/docs/IDE/ |
| Pydantic Support | https://pyrefly.org/en/docs/pydantic/ |
| attrs Support | https://pyrefly.org/en/docs/attrs/ |
| Pytest Support | https://pyrefly.org/en/docs/pytest/ |
| Error Suppressions | https://pyrefly.org/en/docs/error-suppressions/ |
| Error Kinds | https://pyrefly.org/en/docs/error-kinds/ |
| Infer / autotype | https://pyrefly.org/en/docs/autotype/ |
| Coverage / report | https://pyrefly.org/en/docs/report/ |
| Stub Generation | https://pyrefly.org/en/docs/stubgen/ |
| Sandbox | https://pyrefly.org/sandbox/ |

### Blog & talks
| Post | URL | What it covers |
|---|---|---|
| Blog index | https://pyrefly.org/blog/ | All posts |
| Adding Pyrefly to Your Agentic Loop | https://pyrefly.org/blog/pyrefly-agentic-loop/ | Wiring `pyrefly check` into a coding agent via a `Stop` hook (basis for our hook) |
| Talk: Type Checking in Agentic Workflows | https://pyrefly.org/blog/type-checking-agentic-workflows/ | Talk version of the agentic-loop idea |
| Pyrefly v1.1 is here! | https://pyrefly.org/blog/v1.1/ | Latest release: 27% faster, `coverage check --fail-under`, JUnit XML output, `incompatible-comparison`, frozen-dataclass enforcement, refactors |
| Define less, check more: Pyrefly now speaks attrs | https://pyrefly.org/blog/pyrefly-attrs/ | Built-in attrs recognition |
| Are you really expected to run five type-checkers now? | https://pyrefly.org/blog/too-many-type-checkers/ | Positioning: coexistence over replacement; test-suite first |
| Making Type Coverage Visible in Dify's CI | https://pyrefly.org/blog/dify-pyrefly-coverage-ci/ | Two-track CI: blocking excludes + non-blocking coverage-delta PR comments (model for our loop) |
| Third-Party Stubs bundled with Pyrefly | https://pyrefly.org/blog/stubs/ | typeshed + pandas/boto3/matplotlib/sklearn etc. ship in the binary |
| Talk: Tensor Shapes in the Type System | https://pyrefly.org/blog/tensor-shapes-in-the-type-system/ | Shape-aware typing (not used here) |
| Give your Python IDE a Glow-Up with Pyrefly | https://pyrefly.org/blog/2025/09/15/ide-extension/ | The IDE/LSP extension |

### Source & announcement
| What | URL |
|---|---|
| GitHub (pyrefly) | https://github.com/facebook/pyrefly |
| Pre-commit hook | https://github.com/facebook/pyrefly-pre-commit |
| Meta engineering announcement | https://engineering.fb.com/2025/05/15/developer-tools/open-sourcing-pyrefly-a-faster-python-type-checker-written-in-rust/ |

## Capabilities

Each feature with its exact CLI (invoke via `uv run pyrefly …` in this repo).

| Capability | Command / usage | Notes |
|---|---|---|
| **Type check** | `pyrefly check` · `pyrefly check --summarize-errors` | Core checker. CI-friendly output: `--output-format=github`, or `--output-format junit-xml` (v1.1). |
| **Init / migrate config** | `pyrefly init` | Migrates `mypy.ini` / `pyrightconfig.json` / `[tool.mypy]` / `[tool.pyright]`; writes `[tool.pyrefly]` (or `pyrefly.toml`). |
| **Baseline tracking** | `pyrefly check --baseline pyrefly-baseline.json --update-baseline` (snapshot) → `pyrefly check --baseline pyrefly-baseline.json` (report only *new* errors) | Config key `baseline = "pyrefly-baseline.json"`. Marked experimental. Core of our feedback loop. |
| **Bulk error suppression** | `pyrefly suppress` · `pyrefly suppress --comment-location=same-line` · `pyrefly suppress --remove-unused` | Inserts/cleans `# pyrefly: ignore` comments for staged adoption. |
| **Autotype / infer** | `pyrefly infer path/to/file.py` (or a dir) | Inserts inferred annotations. Docs advise manual review, small batches; may surface new errors. |
| **Type coverage** | `pyrefly coverage report <paths>` (JSON) · `pyrefly coverage check <paths> --fail-under 60` | Measures typed / `Any` / untyped for public symbols. Parse with `jq .summary.strict_coverage`. |
| **Stub generation** | `pyrefly` stubgen (see stubgen docs) | Generate `.pyi` stubs for a module/package. |
| **Bundled third-party stubs** | *(automatic)* | Full typeshed + pandas, boto3/botocore, matplotlib, scikit-learn/-image, sympy, vispy, conans ship in the binary — no separate `types-*` installs for defaults. |
| **Language server / IDE** | *(editor extension)* | Autocomplete, refactors (move symbol/module, dict→TypedDict/dataclass/Pydantic), inlay hints, pytest-fixture navigation. See IDE docs. |
| **Pydantic v2 support** | *(automatic, no plugin)* | Validates `BaseModel`, `Field`, `ConfigDict` (frozen/extra), constraints (`gt`/`lt`), `RootModel`, aliases. Requires >= 0.33.0. `alias_generator` unsupported. |
| **attrs support** | *(automatic)* | Recognises attrs-defined classes / generated `__init__`. |
| **pytest support** | *(automatic)* | Understands fixtures/patterns; relevant to `tests/`. |

## How it's wired into this repo

### Dependency
Added as a **uv dev dependency** (pinned in `uv.lock`, like `basedpyright`):

```bash
uv add --dev pyrefly
```

### Config — `pyproject.toml`
A new `[tool.pyrefly]` section; `[tool.basedpyright]` is left untouched.

```toml
[tool.pyrefly]
project-includes = ["src", "tests"]
python-version = "3.11"          # matches .python-version / requires-python floor
```

### justfile targets (standalone — NOT in `lint`/`check`/`default`)

```just
alias pyrefly := check-pyrefly

# pyrefly type check (standalone; only fails on errors new since the baseline)
check-pyrefly:
    uv run pyrefly check --baseline pyrefly-baseline.json --summarize-errors

# refresh the committed baseline after fixing/introducing errors
pyrefly-baseline:
    uv run pyrefly check --baseline pyrefly-baseline.json --update-baseline

# type-coverage report (typed / Any / untyped) as JSON
pyrefly-coverage:
    uv run pyrefly coverage report src tests
```

`just check` (→ `lint` + `test`), `just lint`, and `.github/workflows/ci.yml` are **unchanged** —
Pyrefly cannot break the existing green build.

### Committed baseline — `pyrefly-baseline.json`
A snapshot of today's errors, committed to the repo. `check-pyrefly` reports only regressions
against it; as errors are fixed, regenerate it so the committed count shrinks (visible burn-down).

### Agent hook — `.claude/settings.json`
A `Stop` hook runs Pyrefly against the baseline after each agent turn, so the agent is nudged to fix
**newly-introduced** errors before stopping (pattern from the
[agentic-loop blog](https://pyrefly.org/blog/pyrefly-agentic-loop/) — stdout→stderr, `exit 2`):

```json
"hooks": {
  "Stop": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "cd \"$CLAUDE_PROJECT_DIR\" && uv run pyrefly check --baseline pyrefly-baseline.json >&2 || exit 2",
          "timeout": 30
        }
      ]
    }
  ]
}
```

Disable it by removing the `"hooks"` key from `.claude/settings.json`.

## The feedback loop

The loop tracks typing errors two ways — **error burn-down** (baseline) and **coverage climb** —
mirroring [Dify's CI pattern](https://pyrefly.org/blog/dify-pyrefly-coverage-ci/): reward
incremental, visible progress over a single hard gate.

1. **See regressions** — `just check-pyrefly`: passes on a clean tree, fails only on errors new
   since the committed baseline.
2. **Fix** — resolve reported errors, or `uv run pyrefly infer <path>` to add annotations (review
   the diff; run in small batches).
3. **Burn down** — `just pyrefly-baseline`: regenerate the baseline so the committed error count
   drops. The shrinking `pyrefly-baseline.json` is the progress signal in `git`.
4. **Coverage** — `just pyrefly-coverage`: watch `strict_coverage` (annotated, excluding `Any`)
   climb as annotations are added. Track a delta per change, not a global threshold:
   `uv run pyrefly coverage report src tests | jq .summary.strict_coverage`.
5. **Automation** — the `Stop` hook runs step 1 after every agent turn, prompting fixes for new
   errors automatically.

## Error suppression reference

Inline and bulk suppression (full docs: https://pyrefly.org/en/docs/error-suppressions/):

```python
x: int = "no"        # pyrefly: ignore              — suppress on this line (or the line above)
y: int = "no"        # pyrefly: ignore[bad-return]  — suppress a specific error kind
# pyrefly: ignore-errors                            — file-level: suppress all errors in this file
z: int = "no"        # type: ignore                 — standard convention, also honoured
```

Bulk, for adoption sweeps: `pyrefly suppress` (add), `--comment-location=same-line` (avoid clashing
with tools that write to the preceding line), `--remove-unused` (clean up). Error-kind names come
from the [Error Kinds](https://pyrefly.org/en/docs/error-kinds/) reference.

## IDE / editor

Install the Pyrefly extension for LSP features (autocomplete, go-to, inlay hints, refactors, pytest
fixture navigation). Setup per editor: https://pyrefly.org/en/docs/IDE/ and the announcement post
https://pyrefly.org/blog/2025/09/15/ide-extension/. Editor config should point at the same
`[tool.pyrefly]` in `pyproject.toml` so CLI and IDE agree.

## Future rollout (out of scope now)

- Promote `check-pyrefly` into `just check` + a CI step once the baseline reaches zero.
- Add the [`facebook/pyrefly-pre-commit`](https://github.com/facebook/pyrefly-pre-commit) hook.
- Add a Dify-style non-blocking coverage-delta comment on PRs.
- Decide whether Pyrefly eventually replaces `basedpyright` (or the two stay, per the
  [coexistence rationale](https://pyrefly.org/blog/too-many-type-checkers/)).
