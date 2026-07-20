# helpers.py
#
# Defines TraceHelpersMixin — a mixin class that adds convenience methods to
# ActionTrace for common event kinds.
#
# Why a mixin?
# The helpers (db, http, file, model) are just thin wrappers around
# trace.event(). Putting them in a separate mixin keeps trace.py focused on
# the core trace lifecycle and makes it easy to add more helpers later without
# touching the main ActionTrace class.
#
# Why not standalone functions like trace_db(...)?
# The methods live on the trace object (trace.db(...)) because the trace object
# is the natural scope for recording an event. A standalone function would need
# to look up the active trace from the ContextVar every time it was called,
# which is less readable and hides the relationship between the call and the
# trace it belongs to.
#
# All helpers use "target" as the resource field name. Aliases like "table",
# "url", or "path" are not accepted — the grammar stays consistent across all
# kinds so that traces are uniform regardless of the operation type.

from typing import Any


class TraceHelpersMixin:
    """
    Convenience methods for recording common event kinds on a trace.

    These methods are mixed into ActionTrace. They are thin wrappers around
    trace.event() and exist purely to reduce typing for the most frequent
    operations. They add no new behaviour.

    All methods accept arbitrary keyword arguments (**kwargs) that are passed
    through to trace.event(). This means any field defined in the event schema
    (rows, status, duration_ms, result, etc.) can be provided.
    """

    def db(self, operation: str, target: str, **kwargs: Any) -> None:
        """
        Record a database event.

        Equivalent to: trace.event(kind="db", operation=operation, target=target, ...)

        Args:
            operation: The database operation. Standard values: select, insert,
                       update, delete, upsert, transaction, migration.
            target:    The table or database resource involved. Example: "notes".
            **kwargs:  Additional fields such as rows=1, database="sqlite",
                       safe_query="...", params_shape={...}, duration_ms=12.4.

        Example:
            trace.db(operation="insert", target="notes", rows=1)
        """
        self.event(kind="db", operation=operation, target=target, **kwargs)

    def http(self, operation: str, target: str, **kwargs: Any) -> None:
        """
        Record an HTTP event.

        Equivalent to: trace.event(kind="http", operation=operation, target=target, ...)

        Args:
            operation: The HTTP method or action. Examples: get, post, put, delete.
            target:    The endpoint or service name. Example: "payment-provider".
            **kwargs:  Additional fields such as status_code=200, duration_ms=120.

        Example:
            trace.http(operation="post", target="stripe", status_code=200)
        """
        self.event(kind="http", operation=operation, target=target, **kwargs)

    def file(self, operation: str, target: str, **kwargs: Any) -> None:
        """
        Record a file operation event.

        Equivalent to: trace.event(kind="file", operation=operation, target=target, ...)

        Args:
            operation: The file operation. Examples: read, write, delete, move.
            target:    The file path. Example: "data/notes.json".
            **kwargs:  Additional fields such as bytes_written=1024.

        Example:
            trace.file(operation="write", target="data/notes.json")
        """
        self.event(kind="file", operation=operation, target=target, **kwargs)

    def model(self, operation: str, target: str, **kwargs: Any) -> None:
        """
        Record a model call event.

        Equivalent to: trace.event(kind="model", operation=operation, target=target, ...)

        Args:
            operation: The model operation. Examples: completion, embedding, classification.
            target:    The model name or identifier. Example: "claude-sonnet-5".
            **kwargs:  Additional fields such as tokens_in=1200, tokens_out=300.

        Example:
            trace.model(operation="completion", target="claude-sonnet-5", tokens_in=800)
        """
        self.event(kind="model", operation=operation, target=target, **kwargs)
