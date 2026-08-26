"""The startup refusal on a guessable AGENTS_MCP_JWT_SECRET.

`mcp_server/auth.py` authenticates every MCP tool call by verifying an HS256
JWT against this secret and nothing else — no second factor, no allowlist, no
network assumption. The tools it guards read and write transactions,
accounts, budgets and payees straight against the database, and the chart
publishes the server behind the Gateway when it is enabled.

A placeholder is therefore worse than a weak password: the value is printed
in this repository, so anyone who can read the source can mint a token the
server accepts. Nothing about it is visible at runtime — the pods are
Healthy and the agent works either way. These tests exist so the refusal
cannot be quietly removed, and so the "only when agents are enabled" half
cannot be quietly widened into a check that breaks every non-agent
deployment.
"""
from unittest.mock import patch

import pytest

from app.main import (
    _MIN_MCP_JWT_SECRET_LENGTH,
    _assert_mcp_jwt_secret_is_usable,
)

_GOOD = "x" * _MIN_MCP_JWT_SECRET_LENGTH
_PLACEHOLDERS = ["", "change-me-in-production", "dev-mcp-secret-change-in-production"]


class _AgentSettings:
    def __init__(self, secret: str | None, enabled: bool = True):
        self.mcp_jwt_secret = secret
        self.enabled = enabled


class _AppSettings:
    def __init__(self, debug: bool = False):
        self.debug = debug


# `secret` is Optional because a missing env var arrives as None, and
# one test asserts the guard refuses that instead of raising TypeError.
def _run(secret: str | None, *, enabled: bool = True, debug: bool = False):
    with patch(
        "app.agents.config.get_agent_settings",
        return_value=_AgentSettings(secret, enabled),
    ), patch("app.main.get_settings", return_value=_AppSettings(debug)):
        _assert_mcp_jwt_secret_is_usable()


@pytest.mark.parametrize("secret", _PLACEHOLDERS)
def test_placeholder_secrets_are_refused_when_agents_are_enabled(secret):
    with pytest.raises(RuntimeError, match="AGENTS_MCP_JWT_SECRET"):
        _run(secret)


def test_the_code_level_default_is_among_the_refused_values():
    """The default in `app/agents/config.py`, not a value invented here.

    If upstream changes that default, this test fails rather than the guard
    silently ceasing to cover the value it exists for.
    """
    from app.agents.config import AgentSettings

    default = AgentSettings.model_fields["mcp_jwt_secret"].default
    with pytest.raises(RuntimeError, match="known placeholder"):
        _run(default)


def test_the_chart_default_is_among_the_refused_values():
    """`charts/securo/values.yaml` ships its own distinct placeholder.

    Two different placeholders reach production by two different routes, so
    refusing only the code one would leave the chart's value working.
    """
    with pytest.raises(RuntimeError, match="known placeholder"):
        _run("dev-mcp-secret-change-in-production")


def test_a_short_but_unlisted_secret_is_refused():
    with pytest.raises(RuntimeError, match="shorter than"):
        _run("not-a-listed-placeholder")  # 24 chars, under the floor


def test_a_secret_at_exactly_the_floor_is_accepted():
    _run(_GOOD)


def test_a_strong_secret_is_accepted():
    _run("k" * 128)


@pytest.mark.parametrize("secret", _PLACEHOLDERS)
def test_placeholders_are_ignored_when_agents_are_disabled(secret):
    """With agents off the value signs nothing.

    The router is not mounted and the mcp-server container is not deployed,
    so refusing here would break every deployment that does not use agents —
    which is the default.
    """
    _run(secret, enabled=False)


@pytest.mark.parametrize("secret", _PLACEHOLDERS)
def test_debug_warns_instead_of_refusing(secret, caplog):
    _run(secret, debug=True)
    assert "AGENTS_MCP_JWT_SECRET" in caplog.text


def test_none_is_treated_as_a_placeholder_not_a_crash():
    """A missing env var can arrive as None rather than "".

    `len(None)` would raise TypeError before the guard could report anything
    useful, turning a clear refusal into a confusing traceback.
    """
    with pytest.raises(RuntimeError, match="known placeholder"):
        _run(None)
