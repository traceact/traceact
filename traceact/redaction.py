# redaction.py
#
# The registry of field-name patterns used to redact sensitive values before
# they are stored in a trace record.
#
# Why this lives in its own module:
# Both config.py (to validate TraceConfig(redaction_presets=[...]) at
# construction time) and trace.py (to apply the patterns when sanitising a
# value) need this data. Putting it in either of those modules would create a
# circular import, since trace.py already imports from config.py. A small
# leaf module with no imports from the rest of the package is the simplest
# fix: both sides import from here, and this module depends on nothing.
#
# What "redaction" means here:
# Matching is by FIELD NAME, not by value content. A field named "password" is
# redacted regardless of what it holds; a field named "output" holding an
# actual password string is not caught, because nothing here inspects value
# content. This is a real, documented limitation (see USAGE.md) — it keeps the
# mechanism simple, fast, and false-positive-free, at the cost of not catching
# secrets stored under an unexpected key.

from typing import FrozenSet

# Always active whenever redact_by_default (or a decorator/trace override) is
# True. Chosen to be broad via substring matching: "user_password" matches
# because "password" is a substring, "client_secret" matches "secret", etc.
SENSITIVE_PATTERNS: FrozenSet[str] = frozenset({
    "password", "passwd", "pwd",
    "secret", "token", "api_key", "apikey",
    "private_key", "privatekey",
    "access_key", "accesskey",
    "auth", "credential", "credentials",
    "credit_card", "card_number", "cvv", "ssn",
})

# Opt-in groups layered on top of SENSITIVE_PATTERNS via
# TraceConfig(redaction_presets=["api_keys", "http"]). Not enabled by default —
# enabling one changes nothing for fields already caught by the baseline above
# (substring matching means many of these overlap with it already); each
# preset exists to catch field-naming conventions the baseline doesn't cover.
REDACTION_PRESETS: dict = {
    # Key/token naming conventions not already covered by the "token"/"secret"/
    # "key"-compound substrings in the baseline set.
    "api_keys": frozenset({
        "jwt", "bearer", "signing_key", "encryption_key",
        "hmac_key", "master_key",
    }),
    # Fields commonly found in HTTP request/response payloads and headers.
    "http": frozenset({
        "cookie", "set_cookie", "session_id", "csrf_token",
        "x_forwarded_for", "remote_addr", "client_ip",
    }),
    # Local filesystem paths can reveal a machine's username or directory
    # layout. Not covered by the baseline at all.
    "filesystem_paths": frozenset({
        "path", "filepath", "file_path", "dir", "directory",
        "workdir", "cwd", "home_dir", "homedir",
    }),
    # Environment variable dumps or references. Not covered by the baseline.
    "env_vars": frozenset({
        "env", "environ", "environment", "envvar", "env_var", "dotenv",
    }),
    # Raw AI prompt and response content. Fields commonly found in LLM pipelines
    # where trace payloads must not store verbatim prompt text or model output
    # (privacy, cost, or terms-of-service reasons). Only IDs, hashes, counts,
    # and safe metadata should appear in trace records when this preset is active.
    "ai_prompts": frozenset({
        "raw_prompt", "prompt_content", "prompt_text", "prompt",
        "raw_response", "response_content", "response_text",
        "system_prompt", "system_message",
        "conversation", "message_content", "messages",
        "file_content", "source_excerpt", "context_window",
        "completion", "generation", "output_text",
    }),
}
