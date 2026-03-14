"""Tests for libs.core.redaction — secret redaction for logging."""

from __future__ import annotations

import logging

from libs.core.models import AccountAuth, ProxyConfig
from libs.core.redaction import (
    SecretRedactingFilter,
    configure_logging,
    redact_for_log,
    redact_string,
)


class TestIssue12Acceptance:
    """Issue #12 acceptance: unit test that redaction works for li_at, JSESSIONID, Authorization."""

    def test_redaction_works(self):
        """Redacts all three items required by issue #12: li_at, JSESSIONID, Authorization headers."""
        # Structured data: li_at and JSESSIONID
        out = redact_for_log({"li_at": "cookie_val", "jsessionid": "ajax:session123", "other": "ok"})
        assert out["li_at"] == "[REDACTED]"
        assert out["jsessionid"] == "[REDACTED]"
        assert out["other"] == "ok"
        # Inline string: Authorization header (full value, not just "Bearer")
        s = redact_string("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6")
        assert "eyJhbGc" not in s
        assert "Authorization: [REDACTED]" in s
        # Full pipeline: SecretRedactingFilter scrubs AccountAuth dataclass in log args
        filt = SecretRedactingFilter()
        auth = AccountAuth(li_at="pipeline_secret", jsessionid="pipeline_jsession")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Auth: %s", args=(auth,), exc_info=None,
        )
        filt.filter(record)
        rendered = str(record.args)
        assert "pipeline_secret" not in rendered
        assert "pipeline_jsession" not in rendered


class TestModelReprRedaction:
    """Models with secrets must never expose them via repr/str — defense at the source."""

    def test_account_auth_repr_hides_li_at(self):
        auth = AccountAuth(li_at="SUPER_SECRET_COOKIE", jsessionid="ajax:SECRET_SESSION")
        assert "SUPER_SECRET_COOKIE" not in repr(auth)
        assert "SECRET_SESSION" not in repr(auth)
        assert "[REDACTED]" in repr(auth)

    def test_account_auth_str_hides_secrets(self):
        auth = AccountAuth(li_at="secret_val", jsessionid="ajax:secret_jsid")
        assert "secret_val" not in str(auth)
        assert "secret_jsid" not in str(auth)

    def test_account_auth_fstring_hides_secrets(self):
        auth = AccountAuth(li_at="fstring_secret")
        assert "fstring_secret" not in f"{auth}"

    def test_proxy_config_repr_hides_url(self):
        proxy = ProxyConfig(url="http://user:secretpass@proxy:8080")
        assert "secretpass" not in repr(proxy)
        assert "[REDACTED]" in repr(proxy)

    def test_account_auth_safe_in_logger_without_filter(self):
        """Even without SecretRedactingFilter, logger.info('%s', auth) is safe."""
        import io
        auth = AccountAuth(li_at="no_filter_secret", jsessionid="no_filter_jsid")
        handler = logging.StreamHandler(io.StringIO())
        handler.setFormatter(logging.Formatter("%(message)s"))
        test_logger = logging.getLogger("test_no_filter_repr")
        test_logger.handlers = [handler]
        test_logger.setLevel(logging.DEBUG)
        test_logger.propagate = False
        test_logger.info("Auth: %s", auth)
        output = handler.stream.getvalue()
        assert "no_filter_secret" not in output
        assert "no_filter_jsid" not in output


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
            "url": "http://user:pass@host:8080",
            "safe_key": "visible",
        }
        result = redact_for_log(data)
        for key in ("li_at", "jsessionid", "auth_json", "cookie", "password", "token", "api_key", "proxy_url", "url"):
            assert result[key] == "[REDACTED]", f"{key} should be redacted"
        assert result["safe_key"] == "visible"

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

    def test_redacts_authorization_bearer_full_value(self):
        """Authorization: Bearer <token> must redact the entire token (not just 'Bearer')."""
        result = redact_string("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6")
        assert "eyJhbGc" not in result
        assert "Authorization: [REDACTED]" in result

    def test_redacts_authorization_basic_full_value(self):
        """Authorization: Basic <credentials> must redact the entire value."""
        result = redact_string("Authorization: Basic dXNlcl9wYXNz")
        assert "dXNlcl9wYXNz" not in result
        assert "Authorization: [REDACTED]" in result

    def test_redacts_authorization_token_full_value(self):
        """Authorization: Token <api_key> must redact the entire value."""
        result = redact_string("Authorization: Token abc123secretkey")
        assert "abc123secretkey" not in result
        assert "Authorization: [REDACTED]" in result

    def test_empty_string(self):
        assert redact_string("") == ""


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

    def test_scrubs_dataclass_args_account_auth(self):
        """LogRecord with AccountAuth in args must redact li_at and jsessionid."""
        filt = SecretRedactingFilter()
        auth = AccountAuth(li_at="secret_li_at_val", jsessionid="secret_jsession_val")
        record = self._make_record("Auth: %s", (auth,))
        filt.filter(record)
        rendered = str(record.args)
        assert "secret_li_at_val" not in rendered
        assert "secret_jsession_val" not in rendered
        assert "[REDACTED]" in rendered

    def test_scrubs_dataclass_args_proxy_config(self):
        """LogRecord with ProxyConfig in args must redact url (proxy URL)."""
        filt = SecretRedactingFilter()
        proxy = ProxyConfig(url="http://user:secretpass@proxy.example:8080")
        record = self._make_record("Proxy: %s", (proxy,))
        filt.filter(record)
        rendered = str(record.args)
        assert "secretpass" not in rendered
        assert "user:secretpass" not in rendered
        assert "[REDACTED]" in rendered


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
