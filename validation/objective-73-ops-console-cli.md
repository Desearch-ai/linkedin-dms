# Objective 73 Ops Console and CLI Validation

## Rollback Plan (written before validation changes)
- Stop the local validation API/UI server on `127.0.0.1:8899`.
- Remove validation-only generated files under `validation/artifacts/` and `validation/objective-73-ops-console-cli.md` if this validation must be abandoned.
- Remove validation-only SQLite databases: `validation/*.sqlite*` and the copied local runtime DB `desearch_linkedin_dms.sqlite*`.
- If this lands and needs reverting, run `git revert <task-1398-commit>`; before completion only, use `git restore --staged . && git restore .` from `/tmp/mc-task-1398-linkedin-dms`.
- No production, live LinkedIn, external send, or irreversible state is part of this validation.

## Runtime and Tested Revision
- Working dir: `/tmp/mc-task-1398-linkedin-dms`
- Branch: `task/1398-validate-linkedin-ops-console-ui-and-cli-l`
- Base/upstream tested: `origin/objective/o-73-build-a-wacli.sh-inspired-linkedin-ops-console-w`
- Commit tested before validation artifact: `a4a1f0447107717bb3556a1ced4bced556a43622`
- Dependent task commits present in tested branch:
  - Shared safe operator API/storage (#1395 dependency): `1a3b342 feat(task-60301fde): Build shared safe operator API and storage query layer for LinkedIn Op`
  - CLI suite (#1396 dependency): `91cb6aa feat(task-022ac8cc): Expand LinkedIn DMs CLI into safe operator command suite`
  - UI console (#1397 dependency): `a4a1f04 feat(task-c3dabb71): Build local LinkedIn Ops Console UI for inbox, search, drafts, and acc`
- PR URLs: not present in local dispatcher prompt; validation is commit/branch-based.
- Environment: macOS `15.6.1`, `uv 0.11.6`, Python `3.12.12`, localhost API/UI on `127.0.0.1:8899`.
- Raw environment snapshot: `validation/artifacts/environment.txt`.

## Data Source and Safety Boundary
- Data source: synthetic local SQLite only (`validation/local-test.sqlite`, copied to ignored `desearch_linkedin_dms.sqlite` for FastAPI because current `apps/api/main.py` initializes `Storage()` with the default DB path).
- Synthetic account/thread/message/draft/campaign/outbound rows were seeded by `validation/artifacts/seed_validation_data.py`.
- No real LinkedIn cookies, proxy credentials, browser headers, or account tokens were used.
- No unapproved real LinkedIn sends occurred.
- Live-account sync status: **skipped**. Reason: this task explicitly allows seeded/stored-data proof when live LinkedIn state is unsafe/unavailable; no `credential_ref` or documented approved live account state was provided, and active O-24/state was called out as potentially affecting real sync proof.

## Install and Test Evidence
| Step | Command | Result | Evidence |
|---|---|---:|---|
| Clean dependency install | `uv sync --extra test --extra browser` | pass (`exit_code=0`) | `validation/artifacts/uv-sync.log`, `validation/artifacts/uv-sync.exit` |
| Full test suite | `uv run pytest` | pass: `445 passed in 10.57s` | `validation/artifacts/pytest.log`, `validation/artifacts/pytest.exit` |
| UI test command | `uv run pytest tests/test_ops_console_ui.py` | pass: `2 passed in 0.21s` | `validation/artifacts/ui-pytest.log`, `validation/artifacts/ui-pytest.exit` |
| Static diff check | `git diff --check` | pass (`exit_code=0`) | `validation/artifacts/git-diff-check.log`, `validation/artifacts/git-diff-check.exit` |

UI build note: README documents that the Ops Console is served directly by FastAPI; no npm/Vite build is required.

## Local API/UI Runtime
- Server command: `uv run uvicorn apps.api.main:app --host 127.0.0.1 --port 8899`
- Health evidence: `validation/artifacts/api-health.json` (`http_status=200` in `validation/artifacts/api-health.status`).
- Representative API endpoint outputs were captured by `validation/artifacts/capture_api_evidence.py`; summary is `validation/artifacts/api-evidence-summary.json`.
- API redaction check: `validation/artifacts/api-redaction-check.txt` = `api_redaction_check=passed`.

Representative API files:
- `/ops/status`: `validation/artifacts/api-ops-status.json`
- `/ops/accounts/1/health`: `validation/artifacts/api-account-health.json`
- `/ops/inbox`: `validation/artifacts/api-inbox.json`
- `/ops/search`: `validation/artifacts/api-search.json`
- `/ops/threads/1` + messages: `validation/artifacts/api-thread-detail.json`, `validation/artifacts/api-thread-messages.json`
- `/ops/sync/dry-run`: `validation/artifacts/api-sync-dry-run.json`
- `/ops/campaigns/73/status` + dry-run: `validation/artifacts/api-campaign-status.json`, `validation/artifacts/api-campaign-dry-run.json`
- `/ops/drafts` + `/ops/approvals`: `validation/artifacts/api-drafts.json`, `validation/artifacts/api-approvals.json`
- `/ops/audit`: `validation/artifacts/api-audit.json`
- `/ops/send-approved` refusal: `validation/artifacts/api-send-approved-refusal.json` with `http_status=409`, `external_writes=0`.

## CLI JSON Evidence
Captured by `validation/artifacts/run_cli_evidence.sh` against synthetic DB `validation/local-test.sqlite`.

| Command family | Exact command file | Redacted JSON / stderr evidence | Exit |
|---|---|---|---|
| status | `validation/artifacts/cli-status.cmd` | `validation/artifacts/cli-status.json` | `validation/artifacts/cli-status.exit` |
| auth status | `validation/artifacts/cli-auth-status.cmd` | `validation/artifacts/cli-auth-status.json` | `validation/artifacts/cli-auth-status.exit` |
| sync dry-run | `validation/artifacts/cli-sync-dry-run.cmd` | `validation/artifacts/cli-sync-dry-run.json` | `validation/artifacts/cli-sync-dry-run.exit` |
| inbox | `validation/artifacts/cli-inbox.cmd` | `validation/artifacts/cli-inbox.json` | `validation/artifacts/cli-inbox.exit` |
| search | `validation/artifacts/cli-search.cmd` | `validation/artifacts/cli-search.json` | `validation/artifacts/cli-search.exit` |
| threads list | `validation/artifacts/cli-threads-list.cmd` | `validation/artifacts/cli-threads-list.json` | `validation/artifacts/cli-threads-list.exit` |
| threads show | `validation/artifacts/cli-threads-show.cmd` | `validation/artifacts/cli-threads-show.json` | `validation/artifacts/cli-threads-show.exit` |
| messages list | `validation/artifacts/cli-messages-list.cmd` | `validation/artifacts/cli-messages-list.json` | `validation/artifacts/cli-messages-list.exit` |
| draft-reply | `validation/artifacts/cli-draft-reply.cmd` | `validation/artifacts/cli-draft-reply.json` | `validation/artifacts/cli-draft-reply.exit` |
| campaign status | `validation/artifacts/cli-campaign-status.cmd` | `validation/artifacts/cli-campaign-status.json` | `validation/artifacts/cli-campaign-status.exit` |
| campaign dry-run | `validation/artifacts/cli-campaign-dry-run.cmd` | `validation/artifacts/cli-campaign-dry-run.json` | `validation/artifacts/cli-campaign-dry-run.exit` |
| send refusal: missing approval | `validation/artifacts/cli-send-no-approval.cmd` | `validation/artifacts/cli-send-no-approval.stderr` | expected non-zero (`exit_code=1`) |
| send refusal: approved id but mismatched text hash | `validation/artifacts/cli-send-approved-mismatch.cmd` | `validation/artifacts/cli-send-approved-mismatch.stderr` | expected non-zero (`exit_code=1`) |

CLI redaction check passed: no configured synthetic token, csrf, or raw-cookie marker appears in captured CLI JSON/stderr.

## UI Screenshot Evidence
Captured with Playwright + installed local Chrome while exercising the real FastAPI-served UI at `http://127.0.0.1:8899/console`.

| Required screen | Screenshot |
|---|---|
| Inbox/search | `validation/artifacts/ui-inbox-search.png` |
| Thread detail | `validation/artifacts/ui-thread-detail.png` |
| Account health | `validation/artifacts/ui-account-health-sync-status.png` |
| Draft approval | `validation/artifacts/ui-draft-approval.png` |
| Campaign/sync status | `validation/artifacts/ui-campaign-sync-status.png` |
| Audit/outbound history | `validation/artifacts/ui-audit-outbound-history.png` |
| Full console capture | `validation/artifacts/ui-full-console.png` |

- Screenshot capture script/log: `validation/artifacts/capture_ui_screenshots.py`, `validation/artifacts/ui-screenshots.log`, `validation/artifacts/ui-screenshots.exit`.
- UI network errors: none (`validation/artifacts/ui-screenshot-summary.json`).
- UI redaction check: `validation/artifacts/ui-redaction-check.txt` = `ui_redaction_check=passed`.
- The UI draft approval flow created and approved a **local-only** synthetic draft; the script intentionally never clicked `Send approved draft`.

## Approval Gate / No-Send Proof
- CLI missing approval refusal: `validation/artifacts/cli-send-no-approval.stderr` -> `approval_required`; provider path not reached.
- CLI approved-id mismatch refusal: `validation/artifacts/cli-send-approved-mismatch.stderr` -> `approval_text_mismatch`; provider path not reached.
- API send-approved missing approval refusal: `validation/artifacts/api-send-approved-refusal.json` -> `approved=false`, `status=rejected`, `external_writes=0`, HTTP `409`.
- Campaign dry-run evidence: CLI/API outputs both show `dry_run=true` and `external_writes=0`.
- No command executed a matching approved send; no external LinkedIn write was attempted.

## Command Exit Summary
- Critical commands exited `0`: dependency install, full pytest, UI pytest, seed data generation, CLI evidence runner, API evidence capture, UI screenshot capture.
- Expected non-zero command examples were limited to approval-gate refusal cases and are documented above.
- Irreversible changes: none.
- Secrets in output: none found by redaction checks over API/CLI/UI evidence and final committed validation files (`validation/artifacts/final-redaction-check.txt`).
