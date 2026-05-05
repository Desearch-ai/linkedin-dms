from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.error import URLError

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "extension_preflight.py"


def load_preflight_module():
    spec = importlib.util.spec_from_file_location("extension_preflight", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_documents_operational_knobs(capsys):
    preflight = load_preflight_module()

    with pytest.raises(SystemExit) as exc:
        preflight.main(["--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--cdp-port" in help_text
    assert "--profile-dir" in help_text
    assert "--extension-path" in help_text
    assert "--backend-url" in help_text
    assert "https://www.linkedin.com/messaging/" in help_text


def test_backend_health_failure_is_clear_and_redacted(monkeypatch, capsys, tmp_path):
    preflight = load_preflight_module()

    def fail_fetch_json(_url, timeout_s=5.0):
        raise URLError("connection refused li_at=secret JSESSIONID=secret Authorization: Bearer secret")

    monkeypatch.setattr(preflight, "fetch_json", fail_fetch_json)

    code = preflight.main([
        "--no-open-messaging",
        "--profile-dir",
        str(tmp_path / "profile"),
    ])

    output = capsys.readouterr().out
    assert code == 1
    assert "Backend health failed" in output
    assert "127.0.0.1:8899" in output
    assert "li_at" not in output
    assert "JSESSIONID" not in output
    assert "Authorization" not in output
    assert "secret" not in output


def test_missing_extension_target_fails_with_loaded_message(monkeypatch, capsys, tmp_path):
    preflight = load_preflight_module()

    def fake_fetch_json(url, timeout_s=5.0):
        if url.endswith("/health"):
            return {"ok": True}
        if url.endswith("/json/list"):
            return []
        if url.endswith("/json/version"):
            return {"Browser": "Chrome/125"}
        raise AssertionError(url)

    monkeypatch.setattr(preflight, "fetch_json", fake_fetch_json)

    code = preflight.main([
        "--no-open-messaging",
        "--profile-dir",
        str(tmp_path / "profile"),
    ])

    output = capsys.readouterr().out
    assert code == 1
    assert "extension is not loaded" in output
    assert "Desearch LinkedIn DMs Bridge" in output


def test_success_verifies_expected_extension_target_and_opens_messaging(monkeypatch, capsys, tmp_path):
    preflight = load_preflight_module()
    extension_path = ROOT / "chrome-extension"
    expected_id = preflight.extension_id_for_path(extension_path)
    opened = []

    def fake_fetch_json(url, timeout_s=5.0):
        if url.endswith("/health"):
            return {"ok": True}
        if url.endswith("/json/version"):
            return {"Browser": "Chrome/125"}
        if url.endswith("/json/list"):
            return [
                {
                    "type": "service_worker",
                    "url": f"chrome-extension://{expected_id}/background.js",
                    "title": "Service Worker chrome-extension background.js",
                }
            ]
        raise AssertionError(url)

    def fake_open_cdp_url(host, port, target_url, timeout_s=5.0):
        opened.append((host, port, target_url))
        return {"id": "target-1", "url": target_url}

    monkeypatch.setattr(preflight, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(preflight, "open_cdp_url", fake_open_cdp_url)

    code = preflight.main([
        "--profile-dir",
        str(tmp_path / "profile"),
    ])

    output = capsys.readouterr().out
    assert code == 0
    assert "Backend health OK" in output
    assert "Extension loaded" in output
    assert opened == [("127.0.0.1", 18800, "https://www.linkedin.com/messaging/")]
