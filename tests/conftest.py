"""Shared fixtures for the hermetic test suite."""

from __future__ import annotations

import textwrap

import pytest

SAMPLE_CONFIG = textwrap.dedent(
    """
    profiles:
      default:
        endpoint: http://127.0.0.1:5080
        organization: default
        username: admin@example.com
        password: "Complexpass#123"
      dev:
        endpoint: https://dev.openobserve.com/
        organization: dev-org
        username: dev-user@company.com
        password: dev-password
        timeout: 30
        verify: false
    """
)


@pytest.fixture
def sample_config_path(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE_CONFIG)
    return p
