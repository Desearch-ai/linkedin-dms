"""Tests for libs.core.cookies — cookie string parsing and validation."""

from __future__ import annotations

import pytest

from libs.core.cookies import (
    cookies_to_account_auth,
    detect_and_parse_cookies,
    parse_cookie_json,
    parse_cookie_string,
    validate_li_at,
)


class TestParseCookieString:
    def test_basic_pair(self):
        result = parse_cookie_string("li_at=abc123defg")
        assert result == {"li_at": "abc123defg"}

    def test_both_cookies(self):
        result = parse_cookie_string("li_at=abc123defg; JSESSIONID=ajax:token123")
        assert result == {"li_at": "abc123defg", "JSESSIONID": "ajax:token123"}

    def test_case_insensitive_keys(self):
        result = parse_cookie_string("LI_AT=abc123defg; jsessionid=ajax:token123")
        assert result == {"li_at": "abc123defg", "JSESSIONID": "ajax:token123"}

    def test_extra_whitespace(self):
        result = parse_cookie_string("  li_at = abc123defg ;  JSESSIONID = ajax:token123  ")
        assert result == {"li_at": "abc123defg", "JSESSIONID": "ajax:token123"}

    def test_quoted_value(self):
        result = parse_cookie_string('li_at="abc123defg"')
        assert result == {"li_at": "abc123defg"}

    def test_quoted_jsessionid(self):
        result = parse_cookie_string('li_at=abc123defg; JSESSIONID="ajax:token123"')
        assert result == {"li_at": "abc123defg", "JSESSIONID": "ajax:token123"}

    def test_ignores_unknown_cookies(self):
        result = parse_cookie_string("li_at=abc123defg; _ga=GA1.2.123; JSESSIONID=tok")
        assert result == {"li_at": "abc123defg", "JSESSIONID": "tok"}

    def test_empty_string(self):
        assert parse_cookie_string("") == {}

    def test_no_relevant_cookies(self):
        assert parse_cookie_string("_ga=GA1.2.123; foo=bar") == {}


class TestValidateLiAt:
    def test_valid(self):
        assert validate_li_at("AQEDAWx0Y29va2ll") == "AQEDAWx0Y29va2ll"

    def test_strips_whitespace(self):
        assert validate_li_at("  AQEDAWx0Y29va2ll  ") == "AQEDAWx0Y29va2ll"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_li_at("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_li_at("   ")

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="suspiciously short"):
            validate_li_at("abc")

    def test_spaces_in_value_raises(self):
        with pytest.raises(ValueError, match="must not contain spaces"):
            validate_li_at("abc 123 defg hhh")


class TestCookiesToAccountAuth:
    def test_from_cookie_string(self):
        auth = cookies_to_account_auth("li_at=AQEDAWx0Y29va2ll; JSESSIONID=ajax:tok123")
        assert auth.li_at == "AQEDAWx0Y29va2ll"
        assert auth.jsessionid == "ajax:tok123"

    def test_li_at_only(self):
        auth = cookies_to_account_auth("li_at=AQEDAWx0Y29va2ll")
        assert auth.li_at == "AQEDAWx0Y29va2ll"
        assert auth.jsessionid is None

    def test_from_json_array(self):
        json_input = '[{"name": "li_at", "value": "AQEDAWx0Y29va2ll"}, {"name": "JSESSIONID", "value": "ajax:tok123"}]'
        auth = cookies_to_account_auth(json_input)
        assert auth.li_at == "AQEDAWx0Y29va2ll"
        assert auth.jsessionid == "ajax:tok123"

    def test_from_json_array_quoted_jsessionid(self):
        json_input = '[{"name": "li_at", "value": "AQEDAWx0Y29va2ll"}, {"name": "JSESSIONID", "value": "\\"ajax:tok123\\""}]'
        auth = cookies_to_account_auth(json_input)
        assert auth.li_at == "AQEDAWx0Y29va2ll"
        assert auth.jsessionid == "ajax:tok123"

    def test_missing_li_at_raises(self):
        with pytest.raises(ValueError, match="li_at"):
            cookies_to_account_auth("JSESSIONID=ajax:tok123")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="li_at"):
            cookies_to_account_auth("")


class TestParseCookieJson:
    def test_basic(self):
        data = [{"name": "li_at", "value": "abc123defg"}, {"name": "JSESSIONID", "value": "tok"}]
        result = parse_cookie_json(data)
        assert result == {"li_at": "abc123defg", "JSESSIONID": "tok"}

    def test_case_insensitive_name(self):
        data = [{"name": "LI_AT", "value": "abc123defg"}]
        result = parse_cookie_json(data)
        assert result == {"li_at": "abc123defg"}

    def test_ignores_unknown(self):
        data = [{"name": "li_at", "value": "abc123defg"}, {"name": "_ga", "value": "junk"}]
        result = parse_cookie_json(data)
        assert result == {"li_at": "abc123defg"}

    def test_skips_non_dict_entries(self):
        data = [{"name": "li_at", "value": "abc123defg"}, "garbage", 42]
        result = parse_cookie_json(data)
        assert result == {"li_at": "abc123defg"}

    def test_empty_list(self):
        assert parse_cookie_json([]) == {}

    def test_missing_value_key(self):
        data = [{"name": "li_at"}]
        assert parse_cookie_json(data) == {}

    def test_strips_quoted_jsessionid(self):
        data = [
            {"name": "li_at", "value": "abc123defg"},
            {"name": "JSESSIONID", "value": '"ajax:tok123"'},
        ]
        result = parse_cookie_json(data)
        assert result == {"li_at": "abc123defg", "JSESSIONID": "ajax:tok123"}

    def test_unquoted_jsessionid_unchanged(self):
        data = [
            {"name": "li_at", "value": "abc123defg"},
            {"name": "JSESSIONID", "value": "ajax:tok123"},
        ]
        result = parse_cookie_json(data)
        assert result == {"li_at": "abc123defg", "JSESSIONID": "ajax:tok123"}


class TestDetectAndParseCookies:
    def test_detects_header_string(self):
        result = detect_and_parse_cookies("li_at=abc123defg; JSESSIONID=tok")
        assert result == {"li_at": "abc123defg", "JSESSIONID": "tok"}

    def test_detects_json_array(self):
        json_str = '[{"name": "li_at", "value": "abc123defg"}]'
        result = detect_and_parse_cookies(json_str)
        assert result == {"li_at": "abc123defg"}

    def test_malformed_json_falls_back_to_string(self):
        result = detect_and_parse_cookies("[not valid json")
        assert result == {}  # no valid cookie pairs either

    def test_json_with_whitespace(self):
        json_str = '  [{"name": "li_at", "value": "abc123defg"}]  '
        result = detect_and_parse_cookies(json_str)
        assert result == {"li_at": "abc123defg"}

    def test_json_quoted_jsessionid(self):
        json_str = '[{"name": "li_at", "value": "abc123defg"}, {"name": "JSESSIONID", "value": "\\"ajax:tok\\""}]'
        result = detect_and_parse_cookies(json_str)
        assert result == {"li_at": "abc123defg", "JSESSIONID": "ajax:tok"}

    def test_header_quoted_jsessionid(self):
        result = detect_and_parse_cookies('li_at=abc123defg; JSESSIONID="ajax:tok"')
        assert result == {"li_at": "abc123defg", "JSESSIONID": "ajax:tok"}
