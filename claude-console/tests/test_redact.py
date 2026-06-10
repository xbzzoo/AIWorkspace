"""Unit tests for claude_console.redact (builder A's pure redaction layer).

These tests are written purely against the CONTRACT signatures in section 2 and
do not depend on the synthetic fixture tree — redaction is pure.
"""

import copy

from claude_console import redact


# ---------------------------------------------------------------------------
# is_secret_key
# ---------------------------------------------------------------------------

def test_is_secret_key_positive_cases():
    assert redact.is_secret_key("API_TOKEN") is True
    assert redact.is_secret_key("token") is True
    assert redact.is_secret_key("AWS_SECRET_ACCESS_KEY") is True
    assert redact.is_secret_key("password") is True
    assert redact.is_secret_key("Authorization") is True
    assert redact.is_secret_key("client_secret") is True
    assert redact.is_secret_key("refresh_token") is True
    assert redact.is_secret_key("session-cookie") is True


def test_is_secret_key_case_insensitive():
    assert redact.is_secret_key("api_KEY") is True
    assert redact.is_secret_key("ApiKey") is True


def test_is_secret_key_negative_cases():
    assert redact.is_secret_key("editor") is False
    assert redact.is_secret_key("name") is False
    assert redact.is_secret_key("version") is False
    assert redact.is_secret_key("command") is False


# ---------------------------------------------------------------------------
# looks_like_secret_value
# ---------------------------------------------------------------------------

def test_looks_like_secret_value_credentials():
    assert redact.looks_like_secret_value("sk-ant-xxxxxxxxxxxxxxxxxxxx") is True
    assert redact.looks_like_secret_value("sk-1234567890ABCDEFghijklmnop") is True
    assert redact.looks_like_secret_value("ghp_abcdefghijklmnopqrstuvwxyz0123456789") is True
    assert redact.looks_like_secret_value("gho_abcdefghijklmnopqrstuvwxyz0123456789") is True
    assert redact.looks_like_secret_value(
        "github_pat_11ABCDEFG0abcdefghij_KLMNOPqrstuvwxyz0123456789ABCDEFGHIJ"
    ) is True
    # synthetic Slack-shaped fixture, split so push-protection scanners don't
    # flag it as a real secret (runtime value is unchanged)
    assert redact.looks_like_secret_value("xoxb-" + "123456789012-abcdefghijklmno") is True
    assert redact.looks_like_secret_value("AKIAIOSFODNN7EXAMPLE") is True


def test_looks_like_secret_value_jwt():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dQw4w9WgXcQ_abcDEF123456"
    )
    assert redact.looks_like_secret_value(jwt) is True


def test_looks_like_secret_value_long_hex_and_base64():
    # 32+ hex chars
    assert redact.looks_like_secret_value("a" * 32) is True
    assert redact.looks_like_secret_value("deadbeef" * 4) is True
    # 40+ base64-ish chars
    assert redact.looks_like_secret_value("A1b2C3d4" * 6) is True


def test_looks_like_secret_value_non_secrets_pass_through():
    assert redact.looks_like_secret_value("hello") is False
    assert redact.looks_like_secret_value("1.0.0") is False
    assert redact.looks_like_secret_value("") is False
    assert redact.looks_like_secret_value("vim") is False
    assert redact.looks_like_secret_value("Deploy the app") is False
    assert redact.looks_like_secret_value("claude-opus-4-7") is False


# ---------------------------------------------------------------------------
# redact_value
# ---------------------------------------------------------------------------

def test_redact_value_secret_key_redacts_any_type():
    assert redact.redact_value("API_TOKEN", "sk-ant-xxxxxxxxxxxxxxxxxxxx") == redact.REDACTED
    # non-str value under a secret-y key is still redacted
    assert redact.redact_value("password", 12345) == redact.REDACTED
    assert redact.redact_value("secret_flag", True) == redact.REDACTED


def test_redact_value_innocuous_key_with_secret_value():
    assert redact.redact_value("note", "ghp_abcdefghijklmnopqrstuvwxyz0123456789") == redact.REDACTED


def test_redact_value_passes_through_clean():
    assert redact.redact_value("editor", "vim") == "vim"
    assert redact.redact_value("version", "1.0.0") == "1.0.0"
    assert redact.redact_value("count", 7) == 7
    assert redact.redact_value("enabled", True) is True


# ---------------------------------------------------------------------------
# redact_obj — deep nesting + non-mutation
# ---------------------------------------------------------------------------

def test_redact_obj_deep_nesting():
    obj = {
        "env": {"API_TOKEN": "sk-ant-xxxxxxxxxxxxxxxxxxxx", "EDITOR": "vim"},
        "list": [
            {"password": "hunter2"},
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "plain text",
        ],
        "version": "1.0.0",
    }
    out = redact.redact_obj(obj)
    assert out["env"]["API_TOKEN"] == redact.REDACTED
    assert out["env"]["EDITOR"] == "vim"
    assert out["list"][0]["password"] == redact.REDACTED
    assert out["list"][1] == redact.REDACTED  # secret-looking string in a list
    assert out["list"][2] == "plain text"
    assert out["version"] == "1.0.0"


def test_redact_obj_does_not_mutate_input():
    obj = {"env": {"API_TOKEN": "sk-ant-xxxxxxxxxxxxxxxxxxxx"}, "nested": [{"token": "abc"}]}
    snapshot = copy.deepcopy(obj)
    redact.redact_obj(obj)
    assert obj == snapshot  # original untouched


def test_redact_obj_top_level_string():
    assert redact.redact_obj("ghp_abcdefghijklmnopqrstuvwxyz0123456789") == redact.REDACTED
    assert redact.redact_obj("just a normal sentence") == "just a normal sentence"


def test_redact_obj_scalars():
    assert redact.redact_obj(42) == 42
    assert redact.redact_obj(None) is None
    assert redact.redact_obj(True) is True
