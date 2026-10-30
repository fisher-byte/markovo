from __future__ import annotations

from .credit_amounts import parse_credit_units

DEFAULT_CLIENT_MAX_CREDITS = 15
"""Convenience safety ceiling used only by the human CLI and MCP surfaces."""


def resolve_client_max_credits(value: object = DEFAULT_CLIENT_MAX_CREDITS) -> object:
    """Return a valid client ceiling while preserving an explicitly supplied value."""

    parse_credit_units(value)
    return value
