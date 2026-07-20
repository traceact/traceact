# ids.py
#
# Generates prefixed, human-readable IDs for traces, events, steps, and
# correlation groups.
#
# Why prefixes?
# When you read a raw trace record — in a log file, a terminal, or a debugger
# — a bare UUID tells you nothing about what you are looking at. A prefixed ID
# like "trc_9f3a1c7b2d44" or "evt_184c90aa22af" is immediately identifiable.
# Developers know at a glance whether an ID belongs to a trace, an event, or a
# step without having to cross-reference the field name.
#
# Why 12 hex characters?
# 6 random bytes gives 2^48 (~281 trillion) possible values. Collision risk in
# a single session is negligible. The IDs are short enough to read but long
# enough to be unique across all traces in a single run.

import secrets


def _generate_id(prefix: str) -> str:
    """
    Build a prefixed ID from 6 random bytes encoded as lowercase hex.

    The format is:  {prefix}_{12_hex_chars}
    Example:        trc_9f3a1c7b2d44

    Args:
        prefix: A short string identifying the kind of record (e.g. "trc", "evt").

    Returns:
        A string ID that is unique with overwhelming probability.
    """
    return f"{prefix}_{secrets.token_hex(6)}"


def new_trace_id() -> str:
    """Return a fresh trace ID. Example: trc_9f3a1c7b2d44"""
    return _generate_id("trc")


def new_event_id() -> str:
    """Return a fresh event ID. Example: evt_184c90aa22af"""
    return _generate_id("evt")


def new_step_id() -> str:
    """Return a fresh step ID. Example: stp_62e1aa49c103"""
    return _generate_id("stp")


def new_correlation_id() -> str:
    """
    Return a fresh correlation ID for linking related traces.
    Example: corr_71ac4e19aaf0

    A correlation ID is passed externally (e.g. from a request header or a
    job queue message) rather than generated internally most of the time.
    This function exists for cases where the caller wants to start a new
    correlation group and needs a fresh ID to assign.
    """
    return _generate_id("corr")
