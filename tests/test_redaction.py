"""Tests for libs.core.redaction — secret redaction for logging."""

from __future__ import annotations

import logging
import sys

from libs.core.models import AccountAuth, ProxyConfig
from libs.core.redaction import (
    SecretRedactingFilter,
    configure_logging,
    redact_for_log,
    redact_string,
)
from libs.providers.linkedin.provider import LinkedInProvider


# ---------------------------------------------------------------------------
# redact_for_log (dict/list deep redaction)
# ---------------------------------------------------------------------------

class TestRedactForLog:
    def test_redacts_li_at(self):
        result = redact_for_log({"li_at": "secret_cookie", "label": "test"})
        assert result["li_at"] == "[REDACTED]"
        assert result["label"] == "test"

    def test_redacts_jsessionid(self):
        result = redact_for_log({"jsessionid": "ajax:tok123"})
        assert result["jsessionid"] == "[REDACTED]"

    def test_redacts_multiple_keys(self):
        data = {
            "li_at": "secret",
            "jsessionid": "secret",
            "auth_json": "secret",
            "cookie": "secret",
            "password": "secret",
            "token": "secret",
            "api_key": "secret",
            "proxy_url": "http://proxy",
            "safe_key": "visible",
        }
        result = redact_for_log(data)
        for key in ("li_at", "jsessionid", "auth_json", "cookie", "password", "token", "api_key", "proxy_url"):
            assert result[key] == "[REDACTED]", f"{key} should be redacted"
        assert result["safe_key"] == "visible"

    def test_redacts_url_key(self):
        result = redact_for_log({"url": "http://user:pass@host:8080", "account_id": 1})
        assert result["url"] == "[REDACTED]"
        assert result["account_id"] == 1

    def test_case_insensitive(self):
        result = redact_for_log({"LI_AT": "secret", "Password": "secret"})
        assert result["LI_AT"] == "[REDACTED]"
        assert result["Password"] == "[REDACTED]"

    def test_nested_dict(self):
        result = redact_for_log({"outer": {"li_at": "secret", "ok": True}})
        assert result["outer"]["li_at"] == "[REDACTED]"
        assert result["outer"]["ok"] is True

    def test_nested_list(self):
        result = redact_for_log({"items": [{"li_at": "s1"}, {"li_at": "s2"}]})
        assert result["items"][0]["li_at"] == "[REDACTED]"
        assert result["items"][1]["li_at"] == "[REDACTED]"

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": {"password": "deep_secret", "value": 42}}}}
        result = redact_for_log(data)
        assert result["a"]["b"]["c"]["password"] == "[REDACTED]"
        assert result["a"]["b"]["c"]["value"] == 42

    def test_does_not_mutate_original(self):
        original = {"li_at": "secret", "label": "test"}
        redact_for_log(original)
        assert original["li_at"] == "secret"

    def test_list_input(self):
        result = redact_for_log([{"li_at": "s"}, {"ok": True}])
        assert result[0]["li_at"] == "[REDACTED]"
        assert result[1]["ok"] is True

    def test_non_dict_passthrough(self):
        assert redact_for_log("string") == "string"
        assert redact_for_log(42) == 42
        assert redact_for_log(None) is None

    def test_empty_dict(self):
        assert redact_for_log({}) == {}


# ---------------------------------------------------------------------------
# redact_string (inline pattern scrubbing)
# ---------------------------------------------------------------------------

class TestRedactString:
    def test_redacts_li_at_equals(self):
        assert "li_at=[REDACTED]" in redact_string("cookie: li_at=SECRET_VALUE; path=/")

    def test_redacts_jsessionid_equals(self):
        result = redact_string("JSESSIONID=ajax:csrf_tok_123")
        assert "ajax:csrf_tok_123" not in result
        assert "JSESSIONID=[REDACTED]" in result

    def test_redacts_colon_format(self):
        result = redact_string("token: bearer_abc123")
        assert "bearer_abc123" not in result
        assert "token: [REDACTED]" in result

    def test_preserves_safe_content(self):
        safe = "account_id=42 label=test-account"
        assert redact_string(safe) == safe

    def test_multiple_secrets_in_one_string(self):
        text = "li_at=SECRET1; JSESSIONID=SECRET2"
        result = redact_string(text)
        assert "SECRET1" not in result
        assert "SECRET2" not in result

    def test_case_insensitive(self):
        result = redact_string("LI_AT=secret_value")
        assert "secret_value" not in result

    def test_empty_string(self):
        assert redact_string("") == ""

    # --- Authorization patterns (Bearer / Basic / Token) ---

    def test_redacts_bearer_token_full_value(self):
        result = redact_string("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "Authorization: [REDACTED]" in result

    def test_redacts_basic_auth_full_value(self):
        result = redact_string("Authorization=Basic dXNlcjpwYXNzd29yZA==")
        assert "dXNlcjpwYXNzd29yZA==" not in result
        assert "Authorization=[REDACTED]" in result

    def test_redacts_token_scheme(self):
        result = redact_string("authorization: Token abc123xyz")
        assert "abc123xyz" not in result
        assert "authorization: [REDACTED]" in result

    def test_redacts_authorization_plain_value(self):
        result = redact_string("authorization=plain_secret_value")
        assert "plain_secret_value" not in result

    # --- proxy_url inline pattern ---

    def test_redacts_proxy_url_inline(self):
        result = redact_string("proxy_url=http://user:pass@host:8080")
        assert "user:pass" not in result
        assert "proxy_url=[REDACTED]" in result


# ---------------------------------------------------------------------------
# SecretRedactingFilter
# ---------------------------------------------------------------------------

class TestSecretRedactingFilter:
    def _make_record(self, msg: str, args: tuple | dict | None = None) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg=msg, args=args, exc_info=None,
        )
        return record

    def test_scrubs_msg_string(self):
        filt = SecretRedactingFilter()
        record = self._make_record("Login with li_at=SUPERSECRET done")
        filt.filter(record)
        assert "SUPERSECRET" not in record.msg
        assert "[REDACTED]" in record.msg

    def test_scrubs_string_args(self):
        filt = SecretRedactingFilter()
        record = self._make_record("Auth: %s", ("li_at=SECRETVAL",))
        filt.filter(record)
        assert "SECRETVAL" not in str(record.args)

    def test_scrubs_dict_args(self):
        filt = SecretRedactingFilter()
        # LogRecord unwraps single-element tuple containing a dict
        record = self._make_record("Data: %s", ({"li_at": "secret"},))
        filt.filter(record)
        # After LogRecord unpacking, args is the dict itself
        assert record.args["li_at"] == "[REDACTED]"

    def test_always_returns_true(self):
        filt = SecretRedactingFilter()
        record = self._make_record("safe message")
        assert filt.filter(record) is True

    def test_non_string_args_passthrough(self):
        filt = SecretRedactingFilter()
        record = self._make_record("count: %d", (42,))
        filt.filter(record)
        assert record.args == (42,)

    # --- Dataclass scrubbing in filter args ---

    def test_scrubs_dataclass_in_tuple_args(self):
        filt = SecretRedactingFilter()
        auth = AccountAuth(li_at="SUPERSECRET", jsessionid="ajax:tok")
        record = self._make_record("Auth: %s", (auth,))
        filt.filter(record)
        formatted = record.msg % record.args
        assert "SUPERSECRET" not in formatted
        assert "ajax:tok" not in formatted

    def test_scrubs_proxy_dataclass_in_args(self):
        filt = SecretRedactingFilter()
        proxy = ProxyConfig(url="http://user:pass@proxy:8080")
        record = self._make_record("Proxy: %s", (proxy,))
        filt.filter(record)
        formatted = record.msg % record.args
        assert "user:pass" not in formatted

    # --- Exception traceback scrubbing ---

    def test_scrubs_exc_text(self):
        filt = SecretRedactingFilter()
        record = self._make_record("error occurred")
        record.exc_text = "Traceback (most recent call last):\n  li_at=SUPERSECRET leaked here"
        filt.filter(record)
        assert "SUPERSECRET" not in record.exc_text
        assert "[REDACTED]" in record.exc_text

    def test_scrubs_exc_info_message(self):
        filt = SecretRedactingFilter()
        record = self._make_record("error")
        try:
            raise ValueError("failed with token=SECRETVAL123")
        except ValueError:
            record.exc_info = sys.exc_info()
        filt.filter(record)
        assert "SECRETVAL123" not in str(record.exc_info[1])

    def test_scrubs_exc_info_with_cookie(self):
        filt = SecretRedactingFilter()
        record = self._make_record("request failed")
        try:
            raise RuntimeError("cookie li_at=AQE123secret; JSESSIONID=ajax:csrf456")
        except RuntimeError:
            record.exc_info = sys.exc_info()
        filt.filter(record)
        exc_msg = str(record.exc_info[1])
        assert "AQE123secret" not in exc_msg
        assert "ajax:csrf456" not in exc_msg

    def test_exc_info_none_is_noop(self):
        filt = SecretRedactingFilter()
        record = self._make_record("safe message")
        record.exc_info = None
        filt.filter(record)
        assert record.exc_info is None


# ---------------------------------------------------------------------------
# Model __repr__ redaction (defense-in-depth layer 1)
# ---------------------------------------------------------------------------

class TestModelReprRedaction:
    def test_account_auth_repr_hides_secrets(self):
        auth = AccountAuth(li_at="SUPERSECRET", jsessionid="ajax:tok")
        r = repr(auth)
        assert "SUPERSECRET" not in r
        assert "ajax:tok" not in r
        assert "REDACTED" in r

    def test_account_auth_str_hides_secrets(self):
        auth = AccountAuth(li_at="SUPERSECRET", jsessionid="ajax:tok")
        assert "SUPERSECRET" not in str(auth)

    def test_account_auth_fstring_hides_secrets(self):
        auth = AccountAuth(li_at="SUPERSECRET", jsessionid="ajax:tok")
        assert "SUPERSECRET" not in f"auth is {auth}"

    def test_proxy_config_repr_hides_url(self):
        proxy = ProxyConfig(url="http://user:pass@host:8080")
        r = repr(proxy)
        assert "user:pass" not in r
        assert "REDACTED" in r

    def test_proxy_config_str_hides_url(self):
        proxy = ProxyConfig(url="http://user:pass@host:8080")
        assert "user:pass" not in str(proxy)


# ---------------------------------------------------------------------------
# LinkedInProvider __repr__ redaction
# ---------------------------------------------------------------------------

class TestProviderReprRedaction:
    def test_provider_repr_hides_auth(self):
        auth = AccountAuth(li_at="SUPERSECRET", jsessionid="ajax:tok")
        proxy = ProxyConfig(url="http://user:pass@host:8080")
        provider = LinkedInProvider(auth=auth, proxy=proxy)
        r = repr(provider)
        assert "SUPERSECRET" not in r
        assert "ajax:tok" not in r
        assert "user:pass" not in r
        assert "REDACTED" in r

    def test_provider_repr_no_proxy(self):
        auth = AccountAuth(li_at="SUPERSECRET")
        provider = LinkedInProvider(auth=auth, proxy=None)
        r = repr(provider)
        assert "SUPERSECRET" not in r
        assert "None" in r

    def test_provider_str_hides_auth(self):
        auth = AccountAuth(li_at="SUPERSECRET")
        provider = LinkedInProvider(auth=auth)
        assert "SUPERSECRET" not in str(provider)

    def test_provider_fstring_hides_auth(self):
        auth = AccountAuth(li_at="SUPERSECRET")
        proxy = ProxyConfig(url="http://user:pass@host:8080")
        provider = LinkedInProvider(auth=auth, proxy=proxy)
        msg = f"Using provider: {provider}"
        assert "SUPERSECRET" not in msg
        assert "user:pass" not in msg


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------

class TestConfigureLogging:
    def test_adds_filter_to_root(self):
        root = logging.getLogger()
        original_filters = [f for f in root.filters if isinstance(f, SecretRedactingFilter)]
        original_handlers = len(root.handlers)

        configure_logging()

        new_filters = [f for f in root.filters if isinstance(f, SecretRedactingFilter)]
        assert len(new_filters) >= 1

        # Idempotent: calling again should not add a second filter
        configure_logging()
        after_second = [f for f in root.filters if isinstance(f, SecretRedactingFilter)]
        assert len(after_second) == len(new_filters)
