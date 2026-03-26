"""Tests for ``python -m apps.cli`` sync/send entrypoint."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from apps.cli import __main__ as cli_main
from libs.core.job_runner import SyncResult
from libs.core.models import AccountAuth
from libs.core.storage import Storage


@pytest.fixture
def cli_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "cli.sqlite")


@pytest.fixture
def cli_storage(cli_db_path: str) -> Storage:
    s = Storage(db_path=cli_db_path)
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def account_id(cli_storage: Storage) -> int:
    auth = AccountAuth(li_at="cli-test-li-at", jsessionid=None)
    return cli_storage.create_account(label="cli-test", auth=auth, proxy=None)


def test_cli_sync_stderr_todo_when_provider_raises_not_implemented(
    capsys: pytest.CaptureFixture[str], cli_db_path: str, account_id: int
) -> None:
    rc = cli_main.main(
        ["sync", "--account-id", str(account_id), "--db-path", cli_db_path]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert cli_main._PROVIDER_TODO in err


def test_cli_sync_stdout_json_on_success_with_mocked_empty_threads(
    capsys: pytest.CaptureFixture[str],
    cli_db_path: str,
    account_id: int,
) -> None:
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        inst = MagicMock()
        inst.list_threads.return_value = []
        m_cls.return_value = inst
        rc = cli_main.main(
            ["sync", "--account-id", str(account_id), "--db-path", cli_db_path]
        )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {
        "ok": True,
        "synced_threads": 0,
        "messages_inserted": 0,
        "messages_skipped_duplicate": 0,
        "pages_fetched": 0,
    }
    inst.list_threads.assert_called_once_with()


def test_cli_sync_unknown_account_exits_one(cli_db_path: str) -> None:
    rc = cli_main.main(["sync", "--account-id", "999", "--db-path", cli_db_path])
    assert rc == 1


def test_cli_sync_invalid_account_id_exits_one(cli_db_path: str) -> None:
    rc = cli_main.main(["sync", "--account-id", "0", "--db-path", cli_db_path])
    assert rc == 1


def test_cli_sync_invalid_max_pages_exits_two(cli_db_path: str, account_id: int) -> None:
    rc = cli_main.main(
        [
            "sync",
            "--account-id",
            str(account_id),
            "--db-path",
            cli_db_path,
            "--max-pages-per-thread",
            "0",
        ]
    )
    assert rc == 2


def test_cli_sync_invalid_limit_exits_two(cli_db_path: str, account_id: int) -> None:
    rc = cli_main.main(
        [
            "sync",
            "--account-id",
            str(account_id),
            "--db-path",
            cli_db_path,
            "--limit-per-thread",
            "0",
        ]
    )
    assert rc == 2


def test_cli_sync_exhaust_conflicts_with_max_pages(cli_db_path: str, account_id: int) -> None:
    rc = cli_main.main(
        [
            "sync",
            "--account-id",
            str(account_id),
            "--db-path",
            cli_db_path,
            "--exhaust-pagination",
            "--max-pages-per-thread",
            "2",
        ]
    )
    assert rc == 2


def test_cli_sync_default_max_pages_per_thread_is_one(
    cli_db_path: str, account_id: int
) -> None:
    with patch.object(cli_main, "run_sync") as m_run, patch.object(
        cli_main, "LinkedInProvider"
    ) as m_cls:
        m_cls.return_value = MagicMock()
        m_run.return_value = SyncResult(0, 0, 0, 0)
        rc = cli_main.main(
            ["sync", "--account-id", str(account_id), "--db-path", cli_db_path]
        )
    assert rc == 0
    m_run.assert_called_once()
    assert m_run.call_args.kwargs["max_pages_per_thread"] == 1


def test_cli_sync_exhaust_pagination_passes_none_max_pages(
    cli_db_path: str, account_id: int
) -> None:
    with patch.object(cli_main, "run_sync") as m_run, patch.object(
        cli_main, "LinkedInProvider"
    ) as m_cls:
        m_cls.return_value = MagicMock()
        m_run.return_value = SyncResult(0, 0, 0, 0)
        rc = cli_main.main(
            [
                "sync",
                "--account-id",
                str(account_id),
                "--db-path",
                cli_db_path,
                "--exhaust-pagination",
            ]
        )
    assert rc == 0
    m_run.assert_called_once()
    assert m_run.call_args.kwargs["max_pages_per_thread"] is None


def test_cli_send_stdout_json_on_success(
    capsys: pytest.CaptureFixture[str], cli_db_path: str, account_id: int
) -> None:
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        inst = MagicMock()
        inst.send_message.return_value = "urn:li:msg:1"
        m_cls.return_value = inst
        rc = cli_main.main(
            [
                "send",
                "--account-id",
                str(account_id),
                "--db-path",
                cli_db_path,
                "--recipient",
                "urn:li:fsd_profile:ACoAAA",
                "--text",
                "hello",
            ]
        )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is True
    assert out["platform_message_id"] == "urn:li:msg:1"
    assert out["status"] == "sent"
    assert out["was_duplicate"] is False
    assert "send_id" in out
    inst.send_message.assert_called_once_with(
        recipient="urn:li:fsd_profile:ACoAAA",
        text="hello",
    )


def test_cli_send_passes_idempotency_key(cli_db_path: str, account_id: int) -> None:
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        inst = MagicMock()
        inst.send_message.return_value = "mid"
        m_cls.return_value = inst
        rc = cli_main.main(
            [
                "send",
                "--account-id",
                str(account_id),
                "--db-path",
                cli_db_path,
                "--recipient",
                "r1",
                "--text",
                "t",
                "--idempotency-key",
                "k1",
            ]
        )
    assert rc == 0
    inst.send_message.assert_called_once_with(
        recipient="r1",
        text="t",
    )


def test_cli_send_unknown_account_exits_one(cli_db_path: str) -> None:
    rc = cli_main.main(
        [
            "send",
            "--account-id",
            "42",
            "--db-path",
            cli_db_path,
            "--recipient",
            "r",
            "--text",
            "t",
        ]
    )
    assert rc == 1


def test_cli_send_invalid_account_id_exits_one(cli_db_path: str) -> None:
    rc = cli_main.main(
        [
            "send",
            "--account-id",
            "0",
            "--db-path",
            cli_db_path,
            "--recipient",
            "r",
            "--text",
            "t",
        ]
    )
    assert rc == 1


def test_cli_send_text_exceeds_max_length_exits_one(
    cli_db_path: str, account_id: int
) -> None:
    rc = cli_main.main(
        [
            "send",
            "--account-id",
            str(account_id),
            "--db-path",
            cli_db_path,
            "--recipient",
            "r",
            "--text",
            "x" * 8001,
        ]
    )
    assert rc == 1


def test_cli_send_empty_idempotency_key_exits_one(
    cli_db_path: str, account_id: int
) -> None:
    rc = cli_main.main(
        [
            "send",
            "--account-id",
            str(account_id),
            "--db-path",
            cli_db_path,
            "--recipient",
            "r",
            "--text",
            "t",
            "--idempotency-key",
            "",
        ]
    )
    assert rc == 1


def test_cli_send_http_status_error_exits_one(cli_db_path: str, account_id: int) -> None:
    req = MagicMock()
    resp = MagicMock()
    resp.status_code = 500
    err = httpx.HTTPStatusError("fail", request=req, response=resp)
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        inst = MagicMock()
        inst.send_message.side_effect = err
        m_cls.return_value = inst
        rc = cli_main.main(
            [
                "send",
                "--account-id",
                str(account_id),
                "--db-path",
                cli_db_path,
                "--recipient",
                "r",
                "--text",
                "t",
            ]
        )
    assert rc == 1


def test_cli_send_not_implemented_exits_one(
    capsys: pytest.CaptureFixture[str], cli_db_path: str, account_id: int
) -> None:
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        inst = MagicMock()
        inst.send_message.side_effect = NotImplementedError
        m_cls.return_value = inst
        rc = cli_main.main(
            [
                "send",
                "--account-id",
                str(account_id),
                "--db-path",
                cli_db_path,
                "--recipient",
                "r",
                "--text",
                "t",
            ]
        )
    assert rc == 1
    assert cli_main._PROVIDER_TODO in capsys.readouterr().err


def test_cli_unusable_db_path_exits_one(tmp_path: Path) -> None:
    bad_dir = tmp_path / "not_a_sqlite_file"
    bad_dir.mkdir()
    rc = cli_main.main(
        ["sync", "--account-id", "1", "--db-path", str(bad_dir)]
    )
    assert rc == 1


def test_cli_module_invocation_help_succeeds() -> None:
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root)}
    proc = subprocess.run(
        [sys.executable, "-m", "apps.cli", "--help"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "sync" in proc.stdout
    assert "send" in proc.stdout
