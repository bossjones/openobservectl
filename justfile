# openobservectl — verify, introspect, and import dashboards into OpenObserve over its REST API.
# `just <recipe>`; run from repo root.

default: check

install:
    uv sync

fmt:
    uv run ruff format .

lint:
    uv run ruff format --check .
    uv run ruff check .
    uv run basedpyright
    uv run codespell src tests

test:
    uv run pytest

# full CI gate: lint + test
check: lint test

# smoke-test both entrypoints
help:
    uv run openobservectl --help
    uv run python -m openobservectl --help
