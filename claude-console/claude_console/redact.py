"""Secret redaction. Pure, non-mutating, fully unit-tested.

Goal: never leak secrets through the API. `redact_obj` deep-copies and returns a
new structure; it never mutates its input.
"""

import re

REDACTED = "<REDACTED>"

# Key looks secret-y (case-insensitive substring match on any of these):
SECRET_KEY_PARTS = ("token", "secret", "password", "passwd", "api_key",
                    "apikey", "api-key", "credential", "authorization",
                    "auth_token", "access_key", "private_key", "cookie",
                    "client_secret", "refresh_token", "session_token")

# Precompiled credential-value patterns.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_SK_RE = re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{10,}")
_GH_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")
_GH_PAT_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")
_SLACK_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}")
_AWS_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
# base64/url-safe-ish long blob (>=40 chars); requires mixed-ish charset.
_B64_RE = re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}\b")


def is_secret_key(key: str) -> bool:
    """True if the key name (case-insensitive substring) looks secret-y."""
    if not isinstance(key, str):
        return False
    k = key.lower()
    return any(part in k for part in SECRET_KEY_PARTS)


def looks_like_secret_value(value: str) -> bool:
    """True if a string value looks like a credential, even under an innocuous key.

    Detects sk-/sk-ant-, ghp_/gho_/github_pat_, slack xox*-, JWT eyJ..., AWS
    AKIA..., long hex (>=32), and long base64-ish (>=40). Empty/short/plain
    values pass through.
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if len(v) < 8:
        return False

    if _JWT_RE.search(v):
        return True
    if _SK_RE.search(v):
        return True
    if _GH_RE.search(v) or _GH_PAT_RE.search(v):
        return True
    if _SLACK_RE.search(v):
        return True
    if _AWS_RE.search(v):
        return True
    if _HEX_RE.search(v):
        return True

    # Long base64-ish token: require it be a substantial token (>=40), and avoid
    # flagging ordinary prose. A standalone long token with no whitespace is the
    # strong signal here.
    if " " not in v and len(v) >= 40 and _B64_RE.fullmatch(v):
        # Must contain at least one digit OR mixed case OR url-safe symbol to
        # look token-ish rather than a long lowercase word.
        if (any(c.isdigit() for c in v)
                or (any(c.islower() for c in v) and any(c.isupper() for c in v))
                or any(c in "+/_-=" for c in v)):
            return True
    # Embedded long base64 token inside a larger string.
    for m in _B64_RE.finditer(v):
        tok = m.group(0)
        if len(tok) >= 40 and (any(c.isdigit() for c in tok)
                               or (any(c.islower() for c in tok)
                                   and any(c.isupper() for c in tok))
                               or any(c in "+/_-=" for c in tok)):
            return True
    return False


def redact_value(key, value):
    """Redact a single key/value pair.

    If the key is secret-y -> REDACTED regardless of value type. If the value is
    a string that looks like a credential -> REDACTED. Else unchanged.
    """
    if isinstance(key, str) and is_secret_key(key):
        return REDACTED
    if isinstance(value, str) and looks_like_secret_value(value):
        return REDACTED
    return value


def redact_obj(obj):
    """Deep-copy `obj`, applying redaction at every dict entry and string scalar.

    Returns a NEW structure; never mutates the input. For a top-level (or
    list-element) string not under a key, redact only if it looks like a secret.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and is_secret_key(k):
                out[k] = REDACTED
            else:
                # Recurse first; for plain string values also apply value-pattern.
                if isinstance(v, str):
                    out[k] = REDACTED if looks_like_secret_value(v) else v
                else:
                    out[k] = redact_obj(v)
        return out
    if isinstance(obj, list):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, tuple):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, str):
        return REDACTED if looks_like_secret_value(obj) else obj
    # int / float / bool / None — unchanged.
    return obj
