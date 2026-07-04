"""openobservectl — CLI to verify, introspect, and import dashboards into an OpenObserve instance."""

from __future__ import annotations

__all__ = ["__version__"]

from importlib.metadata import PackageNotFoundError, version

try:  # pragma: no cover - trivial import guard
    __version__: str = version("openobservectl")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    __version__ = "0.0.0"
