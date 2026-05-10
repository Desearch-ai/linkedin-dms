# Objective 73 Ops Console and CLI Validation — Fresh Attempt #2

## Rejection Fix
- Previous QA rejection said `validation/objective-73-ops-console-cli.md` was missing from PR #79. This attempt writes the required markdown artifact at exactly: `/tmp/mc-task-1398-linkedin-dms/validation/objective-73-ops-console-cli.md`.
- The artifact now includes environment/runtime, branch/commit, exact commands, API/CLI JSON evidence, UI screenshot paths, no-send proof, live-account status, rollback, source list, and word count.
- Evidence was regenerated from scratch during this attempt against a freshly removed/reseeded local SQLite runtime DB.

## Rollback Plan (written before validation commands)
- Stop the local validation API/UI server on `127.0.0.1:8899`.
- Remove ignored validation runtime DBs only: `validation/local-test.sqlite*` and `desearch_linkedin_dms.sqlite*`.
- Remove or restore generated validation evidence under `validation/artifacts/` and this markdown file with `git restore validation` if abandoning before commit.
- If the committed task needs reverting after merge, run `git revert <task-1398-commit>`.
- No production, live LinkedIn, external send, or irreversible state is part of this validation.

## Runtime and Tested Revision
- Working dir: `/tmp/mc-task-1398-linkedin-dms`
- Required artifact path used: `/tmp/mc-task-1398-linkedin-dms/validation/objective-73-ops-console-cli.md`
- Branch: `task/1398-validate-linkedin-ops-console-ui-and-cli-l`
- Commit tested before final artifact commit: `1f49875ec3c58a27a65951481d6ded2cbb2b90c4`
- Base/upstream tested: `origin/objective/o-73-build-a-wacli.sh-inspired-linkedin-ops-console-w` at `a4a1f0447107717bb3556a1ced4bced556a43622`
- Dependent task commits present in tested branch:
  - #1395 shared operator API/storage dependency: `1a3b342 feat(task-60301fde): Build shared safe operator API and storage query layer for LinkedIn Op`
  - #1396 CLI suite dependency: `91cb6aa feat(task-022ac8cc): Expand LinkedIn DMs CLI into safe operator command suite`
  - #1397 UI console dependency: `a4a1f04 feat(task-c3dabb71): Build local LinkedIn Ops Console UI for inbox, search, drafts, and acc`
- PRs tested: dependent PR URLs were not available in the local dispatcher prompt or shell auth. The current #1398 dispatcher will create/update the PR automatically from this committed branch; previous rejected PR mentioned by QA was #79.
- Environment snapshot: `validation/artifacts/environment.txt`

```text
timestamp=2026-05-10T14:22:21Z
workdir=/tmp/mc-task-1398-linkedin-dms
branch=task/1398-validate-linkedin-ops-console-ui-and-cli-l
commit=1f49875ec3c58a27a65951481d6ded2cbb2b90c4
base_ref=origin/objective/o-73-build-a-wacli.sh-inspired-linkedin-ops-console-w
base_commit=a4a1f0447107717bb3556a1ced4bced556a43622
ProductName:		macOS;ProductVersion:		15.6.1;BuildVersion:		24G90;
uv 0.11.6 (65950801c 2026-04-09 aarch64-apple-darwin)
Python 3.12.12
```

## Data Source and Safety Boundary
- Data source: synthetic local SQLite only (`validation/local-test.sqlite`, copied to ignored `desearch_linkedin_dms.sqlite` for the FastAPI runtime because current API storage initializes from the default DB path).
- Seed command: `uv run python validation/artifacts/seed_validation_data.py` -> `exit_code=0`; log: `validation/artifacts/seed.log`.
- No real LinkedIn cookies, proxy credentials, browser headers, account tokens, or live account credentials were used.
- No unapproved real LinkedIn sends occurred.
- Live-account sync status: **skipped**. Reason: no `credential_ref` or documented approved live account state was provided, and the task explicitly allows seeded/stored-data proof while O-24/live LinkedIn state may still affect real sync proof.

## Exact Validation Commands and Results
| Step | Command | Exit | Evidence |
|---|---|---:|---|
| Clean runtime DBs | `rm -f validation/local-test.sqlite validation/local-test.sqlite-wal validation/local-test.sqlite-shm desearch_linkedin_dms.sqlite desearch_linkedin_dms.sqlite-wal desearch_linkedin_dms.sqlite-shm` | 0 | ignored local files only |
| Dependency install | `uv sync --extra test --extra browser` | `exit_code=0` | `validation/artifacts/uv-sync.log` |
| Full tests | `uv run pytest` | `exit_code=0` | `validation/artifacts/pytest.log` -> `============================= 445 passed in 9.81s ==============================` |
| UI test command | `uv run pytest tests/test_ops_console_ui.py` | `exit_code=0` | `validation/artifacts/ui-pytest.log` -> `============================== 2 passed in 0.21s ===============================` |
| Seed synthetic data | `uv run python validation/artifacts/seed_validation_data.py` | `exit_code=0` | `validation/artifacts/seed.log` |
| Local API/UI server | `uv run uvicorn apps.api.main:app --host 127.0.0.1 --port 8899` | started, then stopped after capture | `validation/artifacts/api-run.log`, `validation/artifacts/api-run.exit` |
| API evidence capture | `uv run python validation/artifacts/capture_api_evidence.py` | `exit_code=0` | `validation/artifacts/api-evidence-summary.json` (15/15 checks OK) |
| CLI evidence capture | `bash validation/artifacts/run_cli_evidence.sh` | `exit_code=0` | `validation/artifacts/cli-run.log` |
| UI screenshots | `uv run python validation/artifacts/capture_ui_screenshots.py` | `exit_code=0` | `validation/artifacts/ui-screenshot-summary.json` |
| Static diff check | `git diff --check` | `exit_code=0` | `validation/artifacts/git-diff-check.log` |
| Final redaction check | `grep -RIE <secret-like-patterns> validation/artifacts ...` | pass | `validation/artifacts/final-redaction-check.txt` |

UI build note: README lines 98-104 document that the Ops Console is served by FastAPI; no npm/Vite install or build is required.

## API Health and Endpoint Evidence
- Health: `validation/artifacts/api-health.json`, status `validation/artifacts/api-health.status`.
- API capture summary: `validation/artifacts/api-evidence-summary.json`.
- API redaction: `api_redaction_check=passed`.
- Representative endpoints captured:
  - `/ops/status`: `validation/artifacts/api-ops-status.json`
  - `/ops/accounts/1/health`: `validation/artifacts/api-account-health.json`
  - `/ops/inbox`: `validation/artifacts/api-inbox.json`
  - `/ops/search`: `validation/artifacts/api-search.json`
  - `/ops/threads/1` and messages: `validation/artifacts/api-thread-detail.json`, `validation/artifacts/api-thread-messages.json`
  - `/ops/sync/dry-run`: `validation/artifacts/api-sync-dry-run.json`
  - `/ops/campaigns/73/status` and dry-run: `validation/artifacts/api-campaign-status.json`, `validation/artifacts/api-campaign-dry-run.json`
  - `/ops/drafts`, `/ops/approvals`, `/ops/audit`: `validation/artifacts/api-drafts.json`, `validation/artifacts/api-approvals.json`, `validation/artifacts/api-audit.json`
  - `/ops/send-approved` refusal: `validation/artifacts/api-send-approved-refusal.json` with HTTP 409 and `external_writes: 0`.

### Representative API JSON: Send-Approved Refusal
```json
{
  "approved": false,
  "error": {
    "code": "approval_required",
    "message": "Approved draft evidence is required before sending.",
    "redacted": true,
    "retryable": false
  },
  "external_writes": 0,
  "ok": false,
  "status": "rejected"
}
```

## CLI JSON Examples by Required Command Family
All CLI commands ran against synthetic DB `validation/local-test.sqlite`. Full stdout/stderr is committed under `validation/artifacts/cli-*.json` and `validation/artifacts/cli-*.stderr`.

### status
Command: `uv run python -m apps.cli status --db-path validation/local-test.sqlite`
```json
{
  "api": {
    "auth_required": true,
    "configured_url": "http://127.0.0.1:8899",
    "reachable": null
  },
  "counts": {
    "accounts": 1,
    "messages": 3,
    "outbound_sends": 1,
    "pending_approvals": 1,
    "threads": 2
  },
  "db": {
    "path": "validation/local-test.sqlite",
    "reachable": true,
    "schema_version": 5
  },
  "ok": true,
  "service": "linkedin-dms",
  "version": "0.0.1",
  "warnings": []
}
```

### auth status
Command: `uv run python -m apps.cli auth status --account-id 1 --db-path validation/local-test.sqlite`
```json
{
  "account_id": 1,
  "checked_at": "2026-05-10T14:22:33.057700+00:00",
  "error": null,
  "next_action": null,
  "ok": true,
  "session": {
    "expires_at": null,
    "has_browser_context": false,
    "has_jsessionid": true,
    "has_li_at": true
  },
  "status": "ok"
}
```

### sync dry-run
Command: `uv run python -m apps.cli sync --account-id 1 --db-path validation/local-test.sqlite --limit-per-thread 25 --max-pages-per-thread 1 --delay-threads 0 --delay-pages 0 --dry-run`
```json
{
  "account_id": 1,
  "dry_run": true,
  "external_reads": 0,
  "external_writes": 0,
  "ok": true,
  "planned": {
    "delay_between_pages_s": 0.0,
    "delay_between_threads_s": 0.0,
    "limit_per_thread": 25,
    "max_pages_per_thread": 1
  },
  "warnings": []
}
```

### inbox
Command: `uv run python -m apps.cli inbox --account-id 1 --db-path validation/local-test.sqlite --limit 10 --json`
```json
{
  "account_id": 1,
  "ok": true,
  "page": {
    "limit": 10,
    "next_cursor": null
  },
  "threads": [
    {
      "created_at": "2026-05-10T14:22:32.836533+00:00",
      "health": "ready",
      "id": 2,
      "last_direction": "in",
      "last_message_at": "2026-05-10T09:10:00+00:00",
      "last_message_preview": "Can you share the campaign dry-run status and audit trail?",
      "message_count": 1,
      "platform_thread_id": "urn:li:msg_conversation:(synthetic,grace)",
      "thread_id": 2,
      "title": "Grace Hopper (synthetic)",
      "unread": false
    },
    "... 1 more omitted; see artifact file"
  ]
}
```

### search
Command: `uv run python -m apps.cli search --account-id 1 --db-path validation/local-test.sqlite --query Bittensor --direction in --limit 10 --json`
```json
{
  "account_id": 1,
  "filters": {
    "direction": "in",
    "from": null,
    "to": null
  },
  "fts": false,
  "ok": true,
  "page": {
    "limit": 10,
    "next_cursor": null
  },
  "query": "Bittensor",
  "results": [
    {
      "direction": "in",
      "message_id": 1,
      "platform_message_id": "synthetic-msg-ada-1",
      "sender": "Ada Lovelace",
      "sent_at": "2026-05-10T09:00:00+00:00",
      "text_snippet": "Interested in Bittensor subnet ops; synthetic cookie li_at=[REDACTED] should redact.",
      "thread_id": 1,
      "title": "Ada Lovelace (synthetic)"
    }
  ]
}
```

### thread detail
Commands: `uv run python -m apps.cli threads list --account-id 1 --db-path validation/local-test.sqlite --limit 10 --json` and `uv run python -m apps.cli threads show --account-id 1 --thread-id 1 --db-path validation/local-test.sqlite --include-messages --limit 10 --json`
```json
{
  "account_id": 1,
  "messages": [
    {
      "direction": "in",
      "id": 1,
      "platform_message_id": "synthetic-msg-ada-1",
      "raw_available": true,
      "sender": "Ada Lovelace",
      "sent_at": "2026-05-10T09:00:00+00:00",
      "text": "Interested in Bittensor subnet ops; synthetic cookie li_at=[REDACTED] should redact."
    },
    "... 1 more omitted; see artifact file"
  ],
  "ok": true,
  "page": {
    "limit": 10,
    "next_cursor": null
  },
  "thread": {
    "created_at": "2026-05-10T14:22:32.836056+00:00",
    "id": 1,
    "last_message_at": "2026-05-10T09:05:00+00:00",
    "message_count": 2,
    "platform_thread_id": "urn:li:msg_conversation:(synthetic,ada)",
    "title": "Ada Lovelace (synthetic)"
  }
}
```

### messages list
Command: `uv run python -m apps.cli messages list --account-id 1 --thread-id 1 --db-path validation/local-test.sqlite --limit 10 --json`
```json
{
  "account_id": 1,
  "messages": [
    {
      "direction": "in",
      "id": 1,
      "platform_message_id": "synthetic-msg-ada-1",
      "raw_available": true,
      "sender": "Ada Lovelace",
      "sent_at": "2026-05-10T09:00:00+00:00",
      "text": "Interested in Bittensor subnet ops; synthetic cookie li_at=[REDACTED] should redact."
    },
    {
      "direction": "out",
      "id": 2,
      "platform_message_id": "synthetic-msg-ada-2",
      "raw_available": true,
      "sender": "Desearch Operator",
      "sent_at": "2026-05-10T09:05:00+00:00",
      "text": "Thanks Ada \u2014 drafting a local-only follow-up for Objective 73 validation."
    }
  ],
  "ok": true,
  "page": {
    "limit": 10,
    "next_cursor": null
  },
  "thread_id": 1
}
```

### draft-reply
Command: `uv run python -m apps.cli draft-reply --account-id 1 --thread-id 2 --db-path validation/local-test.sqlite --text Synthetic local draft from CLI validation; no external send. --campaign-id 73 --idempotency-key obj73-cli-draft --json`
```json
{
  "account_id": 1,
  "approval_id": "appr_20260510_000002",
  "approval_state": "draft",
  "draft_id": 2,
  "external_writes": 0,
  "idempotency_key": "obj73-cli-draft",
  "ok": true,
  "preview": "Synthetic local draft from CLI validation; no external send.",
  "recipient": "urn:li:msg_conversation:(synthetic,grace)",
  "text_sha256": "4453671943aef2793251f037d484c72a9905f8abba86df2aed12a4ea47b1855b",
  "thread_id": 2
}
```

### campaign dry-run/status
Commands: `uv run python -m apps.cli campaign status --campaign-id 73 --account-id 1 --db-path validation/local-test.sqlite --json` and `uv run python -m apps.cli campaign run --campaign-id 73 --account-id 1 --db-path validation/local-test.sqlite --dry-run --limit 10 --json`
```json
{
  "account_id": 1,
  "campaign_id": 73,
  "last_run": null,
  "ok": true,
  "rate_limit": {
    "daily_cap": 25,
    "next_safe_send_at": null,
    "remaining_today": 25,
    "sent_today": 0
  },
  "state": "active",
  "totals": {
    "approved": 1,
    "drafted": 0,
    "failed": 0,
    "prospects": 1,
    "sent": 0,
    "skipped": 0
  }
}
```
```json
{
  "account_id": 1,
  "campaign_id": 73,
  "dry_run": true,
  "external_writes": 0,
  "ok": true,
  "planned_actions": [],
  "summary": {
    "blocked_missing_auth": 0,
    "blocked_not_approved": 0,
    "blocked_rate_limit": 0,
    "would_send_if_approved": 0
  }
}
```

### send-approved refusal / approved-state behavior
- Missing approval command: `uv run python -m apps.cli send --account-id 1 --recipient urn:li:msg_conversation:(synthetic,ada) --text Refusal proof: missing approval --idempotency-key obj73-refuse-missing --db-path validation/local-test.sqlite` -> expected non-zero `exit_code=1` with stderr `error: approval_required (--approved APPROVAL_ID is required before any live send)`.
- Approved id but mismatched text hash command: `uv run python -m apps.cli send --approved appr_20260510_000001 --account-id 1 --recipient urn:li:msg_conversation:(synthetic,ada) --text Tampered text should fail approval hash before provider --idempotency-key obj73-approved-synthetic --db-path validation/local-test.sqlite` -> expected non-zero `exit_code=1` with stderr `error: approval_text_mismatch`.
- The seeded approved record remained local-only; no command executed a matching approved live send.

## UI Screenshot Evidence
Captured with Playwright and installed local Chrome against real FastAPI-served UI at `http://127.0.0.1:8899/console`.

| Required screen | Screenshot |
|---|---|
| Inbox/search | `validation/artifacts/ui-inbox-search.png` |
| Thread detail | `validation/artifacts/ui-thread-detail.png` |
| Account health | `validation/artifacts/ui-account-health-sync-status.png` |
| Draft approval | `validation/artifacts/ui-draft-approval.png` |
| Campaign/sync status | `validation/artifacts/ui-campaign-sync-status.png` |
| Audit/outbound history | `validation/artifacts/ui-audit-outbound-history.png` |
| Full console capture | `validation/artifacts/ui-full-console.png` |

- UI screenshot script/log: `validation/artifacts/capture_ui_screenshots.py`, `validation/artifacts/ui-screenshots.log`, `validation/artifacts/ui-screenshots.exit`.
- UI network failures: `[]`.
- Browser console note: a favicon `404` was observed in `validation/artifacts/api-run.log`; required `/console`, `/console/assets/*`, and `/ops/*` requests succeeded.
- UI redaction: `ui_redaction_check=passed`.
- The UI draft approval flow created and approved a **local-only** synthetic draft; it intentionally never clicked `Send approved draft`.

## Approval Gate / No-Send Proof
- CLI missing approval refusal: `validation/artifacts/cli-send-no-approval.stderr` -> `approval_required`; expected exit 1; provider send path not reached.
- CLI approved-id mismatch refusal: `validation/artifacts/cli-send-approved-mismatch.stderr` -> `approval_text_mismatch`; expected exit 1; provider send path not reached.
- API send-approved missing approval refusal: `validation/artifacts/api-send-approved-refusal.json` -> `approved=false`, `status=rejected`, `external_writes=0`, HTTP `409`.
- Campaign dry-run proof: `validation/artifacts/cli-campaign-dry-run.json` and `validation/artifacts/api-campaign-dry-run.json` both report `dry_run=true` and `external_writes=0`.
- Explicit confirmation: **no unapproved external sends occurred; no real LinkedIn write was attempted**.

## Source List
- `README.md:98-104` — FastAPI-served Ops Console runtime; no npm/Vite build.
- `README.md:232-258` — CLI command families and approval-gated live send behavior.
- `docs/ops-console-cli-spec.md:661-690` — Objective 73 validation artifact structure and UI screenshot requirements.
- `apps/api/main.py:42-47` — `/console` and static UI mount.
- `apps/api/main.py:896` — `/ops/send-approved` approval-gated API endpoint.
- `apps/api/main.py:967-971` — `/ops/validation/objective-73` validation endpoint.
- `apps/cli/__main__.py:174-185` — `draft-reply` local draft command.
- `apps/cli/__main__.py:202-210` — approval-gated `send` command arguments.
- `apps/cli/__main__.py:573-579` — `send` refusal before provider path without approval.

## Command Exit Summary
- Critical commands exited `0`: dependency install, full pytest, UI pytest, seed data generation, CLI evidence runner, API evidence capture, UI screenshot capture, static diff check.
- Expected non-zero command examples were limited to approval-gate refusal cases and are documented above.
- Irreversible changes: none.
- Secrets in output: `final_redaction_check=passed`.
- Current `git status --short` before final commit contained validation artifact updates only:

```text
M validation/artifacts/api-account-health.json
 M validation/artifacts/api-approvals.json
 M validation/artifacts/api-audit.json
 M validation/artifacts/api-drafts.json
 M validation/artifacts/api-inbox.json
 M validation/artifacts/api-run.log
 M validation/artifacts/api-thread-detail.json
 M validation/artifacts/cli-auth-status.json
 M validation/artifacts/cli-auth-status.stderr
 M validation/artifacts/cli-campaign-dry-run.stderr
 M validation/artifacts/cli-inbox.json
 M validation/artifacts/cli-sync-dry-run.stderr
 M validation/artifacts/cli-threads-list.json
 M validation/artifacts/cli-threads-show.json
 M validation/artifacts/environment.txt
 M validation/artifacts/pytest.log
 M validation/artifacts/seed.log
 M validation/artifacts/ui-account-health-sync-status.png
 M validation/artifacts/ui-audit-outbound-history.png
 M validation/artifacts/ui-campaign-sync-status.png
 M validation/artifacts/ui-draft-approval.png
 M validation/artifacts/ui-full-console.png
 M validation/artifacts/ui-screenshot-summary.json
 M validation/artifacts/uv-sync.log
?? uv.lock
?? validation/artifacts/api-capture.exit
?? validation/artifacts/api-capture.log
?? validation/artifacts/api-redaction-grep.log
?? validation/artifacts/ui-cli-redaction-grep.log
```

## Word Count
- 1640 words
