# GitHub Actions for Pyrefly: adopting dify's PR-comment workflows

Spec for adding **non-blocking, PR-comment-based** Pyrefly CI to `openobservectl`, adapted from
[`langgenius/dify`](https://github.com/langgenius/dify)'s production workflows. This closes the gap
flagged in [`specs/pyrefly.md`](./pyrefly.md)'s "Future rollout" section: *"Add a Dify-style
non-blocking coverage-delta comment on PRs."*

Two independent features, each shipped as a **pair** of workflows:

1. **Type-error diff** — on every PR touching `.py` files, run `pyrefly check` on the PR head and
   on the base branch, diff the two, and post the diff as a PR comment (updated in place on every
   push). Never fails the PR.
2. **Type-coverage diff** — same shape, but compares Pyrefly's `coverage report` summary
   (`% typed`, `% strict-typed`, symbol counts) between PR and base, posting a delta table.

Both features are **informational only** — they never gate `just check` / `just lint` / the
existing `ci.yml`, matching the philosophy already established in `specs/pyrefly.md` ("Pyrefly
cannot break the existing green build").

This spec is split into a **generic adoption template** (§2) usable in any `uv` + Pyrefly Python
repo, and a **concrete instantiation for `openobservectl`** (§3) ready to implement as-is.

## 1. Source material and what was/wasn't adopted

All four workflows and both helper scripts were fetched from `langgenius/dify@main` via
`gh api repos/langgenius/dify/contents/<path>`.

| dify path | Purpose | Disposition |
|---|---|---|
| `.github/workflows/pyrefly-diff.yml` | Runs `pyrefly check` on PR head vs. base ref, filters output through a diagnostics-extraction script, `diff -u`s the two, uploads an artifact, comments directly for same-repo PRs | **Adapted** → `pyrefly-diff.yml` |
| `.github/workflows/pyrefly-diff-comment.yml` | `workflow_run` companion: only fires for **forked** PRs, downloads the artifact, posts/updates the comment with write perms, never checks out PR code | **Adapted** → `pyrefly-diff-comment.yml` |
| `.github/workflows/pyrefly-type-coverage.yml` | Runs `pyrefly report` (JSON) on PR vs. base, renders a base-vs-PR markdown table via a helper script | **Adapted** → `pyrefly-type-coverage.yml` |
| `.github/workflows/pyrefly-type-coverage-comment.yml` | `workflow_run` companion, same fork-safety pattern | **Adapted** → `pyrefly-type-coverage-comment.yml` |
| `api/libs/pyrefly_diagnostics.py` | stdlib-only; keeps only `ERROR `/`WARN(ING) ` headline lines + their `--> path:line:col` location line from raw `pyrefly check` stdout, so diffs are stable instead of full ASCII-art excerpts | **Adopted verbatim**, relocated |
| `api/libs/pyrefly_type_coverage.py` | stdlib-only; parses `{"summary": {...}}` JSON and renders a single-report or base-vs-PR delta markdown table | **Adopted verbatim**, relocated |
| `api/pyproject.toml` `[tool.pyrefly]` | `project-includes=["."]`, excludes `.venv`/`migrations/`, `min-severity="warn"`, `errors.missing-override-decorator="error"`, `infer-with-first-use=true` | **Reference only** — conflicts with our existing `[tool.pyrefly]` (`pyproject.toml:70-72`); not adopted |
| `.github/workflows/main-ci.yml` | Monorepo orchestration: path-filters across `web/`/`api/`/`cli/`/`docker/`, Depot runners, `skip-duplicate-actions` | **Out of scope** — no monorepo analog here; our `ci.yml` already does the equivalent single-job gate |
| `.github/workflows/autofix.yml` | Bot that auto-fixes ruff formatting + a long tail of dify-specific steps (docker-compose generation, ESLint, `ast-grep` SQLAlchemy/`Optional[T]` migrations) | **Out of scope** (explicit decision) — not about typing, and almost entirely monorepo-specific; would need a from-scratch design, not an adaptation |
| `.claude/skills/*` | Dify's Claude Code skills (code review, e2e testing, etc.) | **Out of scope** — unrelated to GitHub Actions or typing |

### Verified compatibility (checked against this repo, 2026-07-03)

- **Coverage JSON shape is compatible as-is.** Dify's workflow calls bare `pyrefly report`; this
  repo's `justfile` uses `pyrefly coverage report src tests` (see `justfile:40-41`). Ran the latter
  locally — output is `{"schema_version": "0.2", "module_reports": [...], "summary": {...}}`, and
  `summary` contains all seven keys `pyrefly_type_coverage.py`'s `parse_summary` requires
  (`n_modules`, `n_typable`, `n_typed`, `n_any`, `n_untyped`, `coverage`, `strict_coverage`), plus
  extras it just ignores. **No code changes needed** — only the command name differs
  (`pyrefly coverage report <paths>`, not `pyrefly report`); use ours.
- **Diagnostics line format not yet verified against a real error.** The baseline
  (`pyrefly-baseline.json`) is currently empty and `pyrefly check` reports 0 new errors, so there's
  no live `ERROR `/`WARN ` line to confirm `pyrefly_diagnostics.py`'s prefix-matching against.
  **Verify this once, at implementation time**, by temporarily introducing a type error and running
  `uv run pyrefly check --baseline pyrefly-baseline.json 2>&1 | uv run python
  scripts/pyrefly_diagnostics.py` — confirm non-empty, readable output — before wiring it into CI.

## 2. Generic adoption template (any uv + Pyrefly repo)

Fill in these placeholders for a given repo:

| Placeholder | Meaning | This repo's value |
|---|---|---|
| `{PYTHON_PATHS}` | `pull_request.paths` filter — dirs whose changes should trigger the workflows | `src/**/*.py`, `tests/**/*.py` |
| `{SYNC_CMD}` | Command to install dev deps | `uv sync --dev` (repo root; use `uv sync --project <subdir> --dev` for a monorepo subproject) |
| `{CHECK_CMD}` | The repo's Pyrefly type-check invocation | `uv run pyrefly check --baseline pyrefly-baseline.json` (omit `--baseline ...` if the repo has none) |
| `{COVERAGE_CMD}` | The repo's Pyrefly coverage invocation | `uv run pyrefly coverage report src tests` |
| `{HELPER_SCRIPT_DIR}` | Where the two helper scripts live | `scripts/` |
| `{RUNNER}` | GitHub Actions runner label | `ubuntu-latest` |

Checklist:

1. Add `{HELPER_SCRIPT_DIR}/pyrefly_diagnostics.py` and `{HELPER_SCRIPT_DIR}/pyrefly_type_coverage.py`
   (copy from dify's `api/libs/`, no behavioral changes needed — both are stdlib-only).
2. Add the four workflow files (§3 gives the concrete versions; swap in this table's placeholders
   for a different repo).
3. Confirm `{CHECK_CMD}` / `{COVERAGE_CMD}` match the repo's actual Pyrefly setup (baseline file
   present or not, coverage paths) — don't copy dify's bare `pyrefly check` / `pyrefly report`
   without checking, per §1's verified-compatibility note.
4. Confirm the `pull_request.paths` filter in both trigger-side workflows actually matches where
   the repo's Python source lives.
5. Confirm each `*-comment.yml`'s `workflow_run.workflows:` list names **exactly** matches the
   paired workflow's top-level `name:` field — this is a silent no-op failure if it drifts.
6. Open a real PR that touches a `.py` file:
   - same-repo PR → both comments should appear directly from the trigger-side jobs.
   - (to test the fork path) a PR from a fork → comments should appear via the `workflow_run`
     companion jobs instead, after the trigger-side jobs complete.
   - push again → confirm each comment **updates in place** (by the `### Pyrefly ...` marker),
     not duplicates.
7. Confirm a PR touching only non-`.py` files (e.g. `README.md`) triggers **neither** workflow.

## 3. Concrete instantiation for `openobservectl`

New files, all under `.github/workflows/` unless noted. Action versions match this repo's existing
`ci.yml` convention (tag-pinned, e.g. `actions/checkout@v4`) — **not** dify's SHA-pins; this was an
explicit decision for a small repo where tag-pinning is a fine tradeoff against pin-update churn.

### 3.1 New files

- `scripts/pyrefly_diagnostics.py` — copied verbatim from dify's `api/libs/pyrefly_diagnostics.py`
  (new top-level `scripts/` dir; nothing currently lives there).
- `scripts/pyrefly_type_coverage.py` — copied verbatim from dify's `api/libs/pyrefly_type_coverage.py`.
- `.github/workflows/pyrefly-diff.yml`
- `.github/workflows/pyrefly-diff-comment.yml`
- `.github/workflows/pyrefly-type-coverage.yml`
- `.github/workflows/pyrefly-type-coverage-comment.yml`

### 3.2 `pyrefly-diff.yml`

Trigger-side job. Runs on `pull_request`, path-filtered, `contents: read` only at the top level
(elevated `issues`/`pull-requests: write` scoped to the job, used only in the same-repo-PR branch).

Adaptations from dify's `pyrefly-diff.yml`:

- `runs-on: depot-ubuntu-24.04` → `runs-on: ubuntu-latest`
- `paths: ['api/**/*.py']` → `paths: ['src/**/*.py', 'tests/**/*.py']`
- `uv sync --project api --dev` → `uv sync --dev` (repo root, no subproject)
- Fetch the diagnostics script from the PR head commit (preserves dify's "use the PR's version of
  the extractor, not an outdated one" trick):
  `git show ${{ github.event.pull_request.head.sha }}:scripts/pyrefly_diagnostics.py > /tmp/pyrefly_diagnostics.py`
- `uv run --directory api --dev pyrefly check` → `uv run pyrefly check --baseline pyrefly-baseline.json`
  (root-level; **with** `--baseline`, since that's this repo's actual invocation — see §1)
- Piping stays the same: `pyrefly check ... 2>&1 | uv run python /tmp/pyrefly_diagnostics.py > /tmp/pyrefly_pr.txt || true`
- Base-branch run: same command, after `git checkout ${{ github.base_ref }}`
- `diff -u /tmp/pyrefly_base.txt /tmp/pyrefly_pr.txt > pyrefly_diff.txt || true` — unchanged
- Artifact upload (`pyrefly_diff.txt` + `pr_number.txt`), `actions/upload-artifact@v4`
- Same-repo comment step: `actions/github-script@v7`, same marker-based
  create-or-update-comment logic (`### Pyrefly Diff`), gated on
  `github.event.pull_request.head.repo.full_name == github.repository && steps.line_count_check.outputs.same == 'false'`

### 3.3 `pyrefly-diff-comment.yml`

`workflow_run` companion — unchanged shape from dify, just the runner:

- `workflows: ['Pyrefly Diff Check']` — must match `pyrefly-diff.yml`'s `name:` exactly
- `runs-on: depot-ubuntu-24.04` → `runs-on: ubuntu-latest`
- Fires only when `github.event.workflow_run.pull_requests[0].head.repo.full_name != github.repository`
  (i.e. only for forked PRs — same-repo PRs already got their comment from §3.2)
- Downloads the `pyrefly_diff` artifact via `actions/github-script@v7`, unzips, posts/updates the
  `### Pyrefly Diff` comment using the PR number saved in the artifact (with a fallback to
  `context.payload.workflow_run.pull_requests[0].number`)
- No checkout of PR code — runs entirely on trusted (base-repo) context, which is what makes it
  safe to grant `issues: write` / `pull-requests: write` here

### 3.4 `pyrefly-type-coverage.yml`

Trigger-side job, mirrors §3.2's structure:

- `runs-on: depot-ubuntu-24.04` → `runs-on: ubuntu-latest`
- `paths: ['api/**/*.py']` → `paths: ['src/**/*.py', 'tests/**/*.py']`
- `uv sync --project api --dev` → `uv sync --dev`
- **Command name change** (§1's verified fix): dify's `uv run --directory api --dev pyrefly report`
  → `uv run pyrefly coverage report src tests` — confirmed to emit a `summary` object with all
  fields `pyrefly_type_coverage.py` expects; no script changes needed, just this command swap
  (both the PR-branch run and the base-branch run)
- Fetch the coverage-rendering script from the **base** branch's commit (dify does this
  deliberately — the renderer itself shouldn't change mid-comparison):
  `git show ${{ github.event.pull_request.base.sha }}:scripts/pyrefly_type_coverage.py > /tmp/pyrefly_type_coverage.py`
  (with the same local-file fallback dify uses, updated to `scripts/pyrefly_type_coverage.py`)
- Render step, artifact upload (`pyrefly_type_coverage`, containing `pr_report.json`,
  `base_report.json`, and the repo-root copy dify duplicates for the fork-workflow path — keep
  that duplication, it's what lets the trusted `workflow_run` job find the file without knowing
  this repo has no `api/` subdir; adjust dify's `api/base_report.json` copy to just
  `base_report.json` at repo root since there's no subproject path to mirror), `pr_number.txt`
- Same-repo comment step: unchanged logic, `### Pyrefly Type Coverage` marker

### 3.5 `pyrefly-type-coverage-comment.yml`

`workflow_run` companion, mirrors §3.3:

- `workflows: ['Pyrefly Type Coverage']` — must match §3.4's `name:` exactly
- `runs-on: depot-ubuntu-24.04` → `runs-on: ubuntu-latest`
- Checks out the **default branch** (trusted code) to get `scripts/pyrefly_type_coverage.py`,
  `uv sync --dev` (no `--project api`)
- Downloads the `pyrefly_type_coverage` artifact, renders via
  `uv run python scripts/pyrefly_type_coverage.py --base base_report.json < pr_report.json`
  (paths simplified — no `api/` prefix), posts/updates the `### Pyrefly Type Coverage` comment

## 4. Acceptance criteria (for the implementation pass)

- `just check` / `just lint` / `.github/workflows/ci.yml` remain **completely unchanged** —
  Pyrefly stays non-blocking, per `specs/pyrefly.md`.
- Opening a PR that edits a file under `src/` or `tests/` produces two PR comments
  (`### Pyrefly Diff`, `### Pyrefly Type Coverage`), each updating in place on subsequent pushes
  rather than duplicating.
- A PR touching only non-Python files triggers none of the four new workflows.
- `scripts/pyrefly_diagnostics.py` produces non-empty, readable output against a real introduced
  type error (the one currently-unverified item from §1) before this is considered done.

## 5. Validation commands

Local dry-runs, before relying on the CI comments:

```bash
# Coverage command + renderer, single-report mode
uv run pyrefly coverage report src tests | uv run python scripts/pyrefly_type_coverage.py

# Diagnostics extractor against current (clean) output — expect empty output, exit 0
uv run pyrefly check --baseline pyrefly-baseline.json 2>&1 | uv run python scripts/pyrefly_diagnostics.py

# Diagnostics extractor against a real error (temporarily break a type, run, then revert)
uv run pyrefly check --baseline pyrefly-baseline.json 2>&1 | uv run python scripts/pyrefly_diagnostics.py
```

```bash
git status --short   # after implementation: only the 6 new files listed in §3.1 should appear
```

## 6. Notes

- This spec produces **no code changes** by itself — it's the blueprint for a future
  implementation pass that adds the 6 files in §3.1.
- Once implemented here, treat §2 as the reusable checklist for adding the same pattern to other
  `uv` + Pyrefly Python projects — swap the placeholder table's values per repo.
- Natural follow-ups once this is running in two or more repos: extract the four workflows into a
  shared `workflow_call`-based reusable workflow repo instead of copy-pasting YAML per project.
  Deferred until there's a second real adopter to design against.
