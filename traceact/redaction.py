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
# Two mechanisms live here:
#
# 1. FIELD-NAME matching (SENSITIVE_PATTERNS, REDACTION_PRESETS): a field
#    named "password" is redacted regardless of what it holds. Simple, fast,
#    false-positive-free — and blind to a secret stored under an unexpected
#    key.
#
# 2. VALUE-PATTERN matching (VALUE_PATTERNS): captured string values are
#    scanned for the wire formats of known credential types — an AWS key is
#    AKIA followed by 16 characters wherever it appears, whatever its field
#    is called. This closes exactly the hole field-name matching leaves: a
#    key pasted into a field named "location", or embedded mid-sentence in
#    free text. Only formats with distinctive, near-unmistakable signatures
#    are listed, which is why this can default to ON; entropy-style guessing
#    (which would catch novel secrets at the price of mangling ordinary
#    hashes and IDs) is deliberately absent.
#
# The value-pattern registry is documented in USAGE.md ("Value-pattern
# redaction") — keep the two in sync when extending it, and extend it when a
# new adapter or provider introduces a credential format not covered here.

import re
from typing import FrozenSet, List, Tuple

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


# ---------------------------------------------------------------------------
# Value patterns — content scanning for known credential formats
# ---------------------------------------------------------------------------
#
# Each entry is (name, compiled regex). The name appears in the placeholder a
# match is replaced with — "[redacted:aws-key]" — so a redaction reads as the
# near-miss it is, not as missing data. Matching replaces only the matched
# substring: prose around an embedded secret survives.
#
# Admission rule for this list: the format must be distinctive enough that a
# match is almost certainly a credential. A 40-character base64 string is not
# admissible (it describes half the hashes in any system); AKIA + 16 chars is.
# When a new provider or tool introduces a keyed format, add it here AND to
# the table in USAGE.md.

VALUE_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    # AWS access key IDs (long-term AKIA..., temporary ASIA...).
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # "sk-" style secret keys: OpenAI (sk-...), Anthropic (sk-ant-...),
    # Stripe (sk_live_..., sk_test_...).
    ("sk-token", re.compile(r"\bsk[-_][A-Za-z0-9_-]{16,}\b")),
    # GitHub tokens: classic (ghp_/gho_/ghu_/ghs_/ghr_) and fine-grained.
    ("github-token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    # Slack tokens (bot, app, user, refresh...).
    ("slack-token", re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{10,}\b")),
    # JSON Web Tokens: three base64url segments, header always starts {"...
    # which encodes to eyJ.
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # PEM private key blocks. The header alone is enough to redact on.
    ("pem-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # Google API keys.
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # Authorization header values captured whole ("Bearer <token>").
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    # Credentials embedded in a URL: scheme://user:password@host.
    ("url-credentials", re.compile(r"://[^/\s:@]{1,128}:[^/\s@]{1,256}@")),
]


def scan_value(text: str) -> str:
    """
    Replace every known credential format in a string with its
    "[redacted:<name>]" placeholder. Text without matches is returned
    unchanged (the common case costs one failed search per pattern).
    """
    for name, pattern in VALUE_PATTERNS:
        if pattern.search(text):
            text = pattern.sub(f"[redacted:{name}]", text)
    return text


def find_value_patterns(text: str) -> List[str]:
    """
    The names of every credential format present in a string, in registry
    order, without modifying anything. Backs `traceact doctor --scan`.
    """
    return [name for name, pattern in VALUE_PATTERNS if pattern.search(text)]
