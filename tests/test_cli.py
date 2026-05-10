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
from libs.core.job_runner import SyncConfig, SyncResult
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
        inst.rate_limit_encountered = False
        m_cls.return_value = inst
        rc = cli_main.main(
            ["sync", "--account-id", str(account_id), "--db-path", cli_db_path]
        )
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is True
    assert out["account_id"] == account_id
    assert out["dry_run"] is False
    assert out["synced_threads"] == 0
    assert out["messages_inserted"] == 0
    assert out["messages_skipped_duplicate"] == 0
    assert out["pages_fetched"] == 0
    assert out["rate_limited"] is False
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
        m_run.return_value = SyncResult(0, 0, 0, 0, False)
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
        m_run.return_value = SyncResult(0, 0, 0, 0, False)
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
    capsys: pytest.CaptureFixture[str], cli_db_path: str, cli_storage: Storage, account_id: int
) -> None:
    draft = cli_storage.create_draft_reply(
        account_id=account_id,
        thread_id=None,
        recipient="urn:li:fsd_profile:ACoAAA",
        text="hello",
        idempotency_key=None,
    )
    cli_storage.approve_send_approval(draft["approval_id"], approved_by="test")
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        inst = MagicMock()
        inst.send_message.return_value = "urn:li:msg:1"
        m_cls.return_value = inst
        rc = cli_main.main(
            [
                "send",
                "--approved",
                draft["approval_id"],
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
    assert out["approved"] is True
    assert out["approval_id"] == draft["approval_id"]
    assert out["platform_message_id"] == "urn:li:msg:1"
    assert out["status"] == "sent"
    assert out["was_duplicate"] is False
    assert "send_id" in out
    inst.send_message.assert_called_once_with(
        recipient="urn:li:fsd_profile:ACoAAA",
        text="hello",
    )


def test_cli_send_passes_idempotency_key(cli_db_path: str, cli_storage: Storage, account_id: int) -> None:
    draft = cli_storage.create_draft_reply(
        account_id=account_id,
        thread_id=None,
        recipient="r1",
        text="t",
        idempotency_key="k1",
    )
    cli_storage.approve_send_approval(draft["approval_id"], approved_by="test")
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        inst = MagicMock()
        inst.send_message.return_value = "mid"
        m_cls.return_value = inst
        rc = cli_main.main(
            [
                "send",
                "--approved",
                draft["approval_id"],
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
    capsys: pytest.CaptureFixture[str], cli_db_path: str, cli_storage: Storage, account_id: int
) -> None:
    draft = cli_storage.create_draft_reply(
        account_id=account_id,
        thread_id=None,
        recipient="r",
        text="t",
        idempotency_key=None,
    )
    cli_storage.approve_send_approval(draft["approval_id"], approved_by="test")
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        inst = MagicMock()
        inst.send_message.side_effect = NotImplementedError
        m_cls.return_value = inst
        rc = cli_main.main(
            [
                "send",
                "--approved",
                draft["approval_id"],
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



def _seed_cli_messages(storage: Storage, account_id: int) -> int:
    from datetime import datetime, timezone

    thread_id = storage.upsert_thread(
        account_id=account_id,
        platform_thread_id="urn:li:msg_conversation:ada",
        title="Ada Lovelace",
    )
    storage.insert_message(
        account_id=account_id,
        thread_id=thread_id,
        platform_message_id="msg-1",
        direction="in",
        sender="Ada",
        text="Bittensor launch details li_at=AQED_DO_NOT_LEAK",
        sent_at=datetime(2026, 5, 8, 9, 30, tzinfo=timezone.utc),
        raw={"safe": True},
    )
    storage.insert_message(
        account_id=account_id,
        thread_id=thread_id,
        platform_message_id="msg-2",
        direction="out",
        sender=None,
        text="Thanks, this is helpful.",
        sent_at=datetime(2026, 5, 10, 11, 55, tzinfo=timezone.utc),
    )
    return thread_id


def test_cli_help_lists_operator_command_families(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main._parse_args(["--help"])
    assert exc.value.code == 0
    help_out = capsys.readouterr().out
    for command in ["status", "auth", "sync", "inbox", "search", "threads", "messages", "draft-reply", "campaign", "send"]:
        assert command in help_out


def test_cli_status_json_empty_db(capsys: pytest.CaptureFixture[str], cli_db_path: str) -> None:
    rc = cli_main.main(["status", "--db-path", cli_db_path])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["service"] == "linkedin-dms"
    assert payload["counts"] == {
        "accounts": 0,
        "threads": 0,
        "messages": 0,
        "pending_approvals": 0,
        "outbound_sends": 0,
    }


def test_cli_auth_status_unknown_account_exits_one(cli_db_path: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main.main(["auth", "status", "--account-id", "999", "--db-path", cli_db_path])
    assert rc == 1
    assert "account 999 not found" in capsys.readouterr().err


def test_cli_inbox_search_thread_and_message_json_outputs(
    capsys: pytest.CaptureFixture[str], cli_db_path: str, cli_storage: Storage, account_id: int
) -> None:
    thread_id = _seed_cli_messages(cli_storage, account_id)

    assert cli_main.main(["inbox", "--account-id", str(account_id), "--db-path", cli_db_path, "--json"]) == 0
    inbox = json.loads(capsys.readouterr().out)
    assert inbox["threads"][0]["thread_id"] == thread_id
    assert "AQED_DO_NOT_LEAK" not in str(inbox)

    assert cli_main.main([
        "search", "--account-id", str(account_id), "--db-path", cli_db_path, "--query", "bittensor", "--json"
    ]) == 0
    search = json.loads(capsys.readouterr().out)
    assert search["results"][0]["thread_id"] == thread_id
    assert "AQED_DO_NOT_LEAK" not in str(search)

    assert cli_main.main([
        "threads", "show", "--account-id", str(account_id), "--thread-id", str(thread_id),
        "--db-path", cli_db_path, "--include-messages", "--json"
    ]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["thread"]["id"] == thread_id
    assert detail["messages"][0]["raw_available"] is True

    assert cli_main.main([
        "messages", "list", "--account-id", str(account_id), "--thread-id", str(thread_id), "--db-path", cli_db_path, "--json"
    ]) == 0
    messages = json.loads(capsys.readouterr().out)
    assert messages["messages"][0]["platform_message_id"] == "msg-1"


def test_cli_draft_reply_creates_local_draft_without_sending(
    capsys: pytest.CaptureFixture[str], cli_db_path: str, account_id: int
) -> None:
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        rc = cli_main.main([
            "draft-reply", "--account-id", str(account_id), "--recipient", "urn:li:member:1",
            "--text", "hello", "--idempotency-key", "idem-1", "--db-path", cli_db_path,
        ])
    assert rc == 0
    draft = json.loads(capsys.readouterr().out)
    assert draft["approval_state"] == "draft"
    assert draft["external_writes"] == 0
    m_cls.assert_not_called()


def test_cli_campaign_run_requires_and_reports_dry_run(
    capsys: pytest.CaptureFixture[str], cli_db_path: str, account_id: int
) -> None:
    rc = cli_main.main(["campaign", "run", "--campaign-id", "123", "--account-id", str(account_id), "--db-path", cli_db_path])
    assert rc == 2

    rc = cli_main.main([
        "campaign", "run", "--campaign-id", "123", "--account-id", str(account_id), "--db-path", cli_db_path, "--dry-run"
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["external_writes"] == 0
    assert payload["planned_actions"] == []


def test_cli_send_refuses_without_approved_state(
    capsys: pytest.CaptureFixture[str], cli_db_path: str, account_id: int
) -> None:
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        rc = cli_main.main([
            "send", "--account-id", str(account_id), "--db-path", cli_db_path,
            "--recipient", "r", "--text", "t",
        ])
    assert rc == 1
    assert "approval_required" in capsys.readouterr().err
    m_cls.assert_not_called()


def test_cli_send_with_approved_draft_executes_and_marks_used(
    capsys: pytest.CaptureFixture[str], cli_db_path: str, cli_storage: Storage, account_id: int
) -> None:
    draft = cli_storage.create_draft_reply(
        account_id=account_id,
        thread_id=None,
        recipient="r",
        text="t",
        idempotency_key="idem-approved",
    )
    cli_storage.approve_send_approval(draft["approval_id"], approved_by="test")
    with patch.object(cli_main, "LinkedInProvider") as m_cls:
        inst = MagicMock()
        inst.send_message.return_value = "mid-approved"
        m_cls.return_value = inst
        rc = cli_main.main([
            "send", "--approved", draft["approval_id"], "--account-id", str(account_id), "--db-path", cli_db_path,
            "--recipient", "r", "--text", "t", "--idempotency-key", "idem-approved",
        ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["approved"] is True
    assert payload["approval_id"] == draft["approval_id"]
    assert cli_storage.get_approval(draft["approval_id"])["state"] == "used"
    inst.send_message.assert_called_once_with(recipient="r", text="t")
