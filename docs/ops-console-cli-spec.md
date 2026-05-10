# LinkedIn Ops Console and CLI Specification

## 1. Product promise

The LinkedIn Ops Console and CLI give operators a safe, inspectable control plane for LinkedIn DM operations backed by the existing FastAPI, SQLite, and provider stack. The promise is:

- show account health before an operator tries to sync or send;
- sync LinkedIn conversations into local storage with clear progress, rate-limit, and error reporting;
- browse, search, and inspect inbox threads and messages without touching LinkedIn;
- draft outbound replies and campaign messages in a local approval queue;
- execute sends only after an explicit `approved` state is recorded;
- leave an audit trail that can be attached to Objective 73 validation.

This spec is source-driven from the current repository:

- `README.md` describes the existing FastAPI + SQLite + CLI product surface and current endpoints.
- `apps/cli/__main__.py` currently implements `sync` and `send` commands with JSON output.
- `apps/api/main.py` currently exposes account, auth, thread, sync, ingest, send, and sends endpoints.
- `libs/core/storage.py` currently persists accounts, threads, messages, sync cursors, browser context, and outbound sends.

## 2. Non-goals

- No autonomous LinkedIn outreach. Campaign runs default to local dry-run planning and never send without approved records.
- No bypass of LinkedIn auth, anti-bot systems, rate limits, or account safety controls.
- No unredacted display, logging, export, or API response of cookies, CSRF tokens, proxy credentials, bearer tokens, or raw browser headers.
- No public multi-tenant SaaS surface in this phase. The API remains local-first and bearer-token protected when exposed to other local processes.
- No replacement of the provider abstraction. The console and CLI orchestrate the existing provider/storage layers and future-compatible endpoints.
- No destructive bulk deletion flows. Data cleanup, if added later, needs its own spec and approval gate.

## 3. CLI command tree

The operator-facing executable name can be `linkedin-dms` after packaging. During repository development every command maps to `python -m apps.cli` plus the selected subcommand or a future wrapper that calls the same handlers.

```text
linkedin-dms
├── status
│   └── --db-path PATH
├── auth
│   └── status --account-id ID [--db-path PATH]
├── sync
│   ├── --account-id ID
│   ├── --db-path PATH
│   ├── --limit-per-thread N
│   ├── --max-pages-per-thread N
│   ├── --exhaust-pagination
│   ├── --delay-threads SEC
│   ├── --delay-pages SEC
│   └── --dry-run
├── inbox
│   ├── --account-id ID
│   ├── --db-path PATH
│   ├── --limit N
│   ├── --cursor CURSOR
│   ├── --unread-only
│   └── --json
├── search
│   ├── --account-id ID
│   ├── --query TEXT
│   ├── --db-path PATH
│   ├── --from YYYY-MM-DD
│   ├── --to YYYY-MM-DD
│   ├── --direction in|out
│   ├── --limit N
│   └── --json
├── threads
│   ├── list --account-id ID [--db-path PATH] [--limit N] [--cursor CURSOR]
│   └── show --thread-id ID [--account-id ID] [--db-path PATH] [--include-messages] [--limit N]
├── messages
│   └── list --thread-id ID [--account-id ID] [--db-path PATH] [--limit N] [--cursor CURSOR]
├── draft-reply
│   ├── --account-id ID
│   ├── --thread-id ID | --recipient URN_OR_CONV_ID
│   ├── --text BODY | --text-file PATH
│   ├── --campaign-id ID
│   ├── --idempotency-key KEY
│   └── --db-path PATH
├── campaign
│   ├── status --campaign-id ID [--account-id ID] [--db-path PATH]
│   └── run --campaign-id ID --dry-run [--account-id ID] [--db-path PATH] [--limit N]
└── send
    ├── --approved APPROVAL_ID
    ├── --account-id ID
    ├── --recipient URN_OR_CONV_ID
    ├── --text BODY | --draft-id ID
    ├── --idempotency-key KEY
    └── --db-path PATH
```

### 3.1 Command behavior rules

- `status`, `auth status`, `inbox`, `search`, `threads`, `messages`, `draft-reply`, and `campaign status` are local read or local write operations only. They must not send external LinkedIn messages.
- `sync` may read from LinkedIn. It uses conservative pagination defaults already present in the API/CLI. `--dry-run` validates account/provider readiness and reports planned pagination without writing fetched data.
- `campaign run` requires `--dry-run` in this phase. It creates a deterministic local plan but does not call the provider send path.
- `send` refuses to run unless `--approved APPROVAL_ID` references an approval record in state `approved` for the exact account, recipient, text hash, and idempotency key. Passing message text alone is not approval.
- Every command emits one JSON object to stdout on success. Human-readable progress, warnings, and errors go to stderr.

## 4. JSON output schemas

Schemas below are normative for CLI stdout. API responses should use the same field names where the same concept exists.

### 4.1 `status`

```json
{
  "ok": true,
  "service": "linkedin-dms",
  "version": "0.0.2",
  "db": {
    "path": "./desearch_linkedin_dms.sqlite",
    "reachable": true,
    "schema_version": 4
  },
  "api": {
    "configured_url": "http://127.0.0.1:8899",
    "reachable": true,
    "auth_required": true
  },
  "counts": {
    "accounts": 1,
    "threads": 42,
    "messages": 1200,
    "pending_approvals": 3,
    "outbound_sends": 7
  },
  "warnings": []
}
```

### 4.2 `auth status`

```json
{
  "ok": true,
  "account_id": 1,
  "status": "ok",
  "checked_at": "2026-05-10T12:00:00Z",
  "session": {
    "has_li_at": true,
    "has_jsessionid": true,
    "has_browser_context": true,
    "expires_at": null
  },
  "error": null,
  "next_action": null
}
```

If auth fails, `ok` is `false`, `status` is `failed`, `error` is redacted, and `next_action` is `refresh_account`.

### 4.3 `sync`

Current implementation already emits the core success payload. The console-compatible schema extends it with account and timing metadata:

```json
{
  "ok": true,
  "account_id": 1,
  "dry_run": false,
  "synced_threads": 12,
  "messages_inserted": 85,
  "messages_skipped_duplicate": 240,
  "pages_fetched": 12,
  "rate_limited": false,
  "started_at": "2026-05-10T12:00:00Z",
  "finished_at": "2026-05-10T12:00:14Z",
  "warnings": []
}
```

For dry-run:

```json
{
  "ok": true,
  "account_id": 1,
  "dry_run": true,
  "planned": {
    "limit_per_thread": 50,
    "max_pages_per_thread": 1,
    "delay_between_threads_s": 2.0,
    "delay_between_pages_s": 1.5
  },
  "external_reads": 0,
  "external_writes": 0,
  "warnings": []
}
```

### 4.4 `inbox`

```json
{
  "ok": true,
  "account_id": 1,
  "threads": [
    {
      "thread_id": 10,
      "platform_thread_id": "urn:li:msg_conversation:(urn:li:fsd_profile:me,abc)",
      "title": "Ada Lovelace",
      "last_message_at": "2026-05-10T11:55:00Z",
      "last_message_preview": "Thanks, this is helpful.",
      "last_direction": "in",
      "message_count": 14,
      "unread": false,
      "health": "ready"
    }
  ],
  "page": {
    "limit": 25,
    "next_cursor": "eyJpZCI6MTB9"
  }
}
```

### 4.5 `search`

```json
{
  "ok": true,
  "account_id": 1,
  "query": "bittensor",
  "filters": {
    "from": "2026-05-01",
    "to": "2026-05-10",
    "direction": "in"
  },
  "results": [
    {
      "message_id": 77,
      "thread_id": 10,
      "platform_message_id": "urn:li:msg:123",
      "title": "Ada Lovelace",
      "direction": "in",
      "sender": "Ada Lovelace",
      "text_snippet": "...Bittensor subnet launch...",
      "sent_at": "2026-05-08T09:30:00Z"
    }
  ],
  "page": {
    "limit": 25,
    "next_cursor": null
  }
}
```

### 4.6 `threads list` and `threads show`

`threads list`:

```json
{
  "ok": true,
  "account_id": 1,
  "threads": [
    {
      "id": 10,
      "platform_thread_id": "urn:li:msg_conversation:(urn:li:fsd_profile:me,abc)",
      "title": "Ada Lovelace",
      "created_at": "2026-05-10T10:00:00Z",
      "message_count": 14,
      "last_message_at": "2026-05-10T11:55:00Z"
    }
  ],
  "page": {
    "limit": 25,
    "next_cursor": null
  }
}
```

`threads show --include-messages`:

```json
{
  "ok": true,
  "account_id": 1,
  "thread": {
    "id": 10,
    "platform_thread_id": "urn:li:msg_conversation:(urn:li:fsd_profile:me,abc)",
    "title": "Ada Lovelace",
    "created_at": "2026-05-10T10:00:00Z"
  },
  "messages": [
    {
      "id": 77,
      "platform_message_id": "urn:li:msg:123",
      "direction": "in",
      "sender": "Ada Lovelace",
      "text": "Thanks, this is helpful.",
      "sent_at": "2026-05-10T11:55:00Z"
    }
  ],
  "page": {
    "limit": 50,
    "next_cursor": null
  }
}
```

### 4.7 `messages list`

```json
{
  "ok": true,
  "account_id": 1,
  "thread_id": 10,
  "messages": [
    {
      "id": 77,
      "platform_message_id": "urn:li:msg:123",
      "direction": "in",
      "sender": "Ada Lovelace",
      "text": "Thanks, this is helpful.",
      "sent_at": "2026-05-10T11:55:00Z",
      "raw_available": true
    }
  ],
  "page": {
    "limit": 50,
    "next_cursor": null
  }
}
```

### 4.8 `draft-reply`

`draft-reply` creates a local approval candidate. It is not a send operation.

```json
{
  "ok": true,
  "draft_id": 31,
  "approval_id": "appr_20260510_000031",
  "approval_state": "draft",
  "account_id": 1,
  "thread_id": 10,
  "recipient": "urn:li:fsd_profile:123",
  "text_sha256": "1b73c3f5a0ec...",
  "preview": "Great to hear from you. Would next Tuesday work?",
  "idempotency_key": "linkedin-dm-2026-05-10-031",
  "external_writes": 0
}
```

### 4.9 `campaign status`

```json
{
  "ok": true,
  "campaign_id": 5,
  "account_id": 1,
  "state": "draft",
  "totals": {
    "prospects": 100,
    "drafted": 80,
    "approved": 12,
    "sent": 7,
    "failed": 1,
    "skipped": 0
  },
  "rate_limit": {
    "daily_cap": 25,
    "sent_today": 7,
    "remaining_today": 18,
    "next_safe_send_at": "2026-05-10T12:20:00Z"
  },
  "last_run": {
    "dry_run": true,
    "finished_at": "2026-05-10T11:00:00Z"
  }
}
```

### 4.10 `campaign run --dry-run`

```json
{
  "ok": true,
  "campaign_id": 5,
  "account_id": 1,
  "dry_run": true,
  "external_writes": 0,
  "planned_actions": [
    {
      "recipient": "urn:li:fsd_profile:123",
      "draft_id": 31,
      "approval_state": "draft",
      "would_send": false,
      "reason": "not_approved"
    }
  ],
  "summary": {
    "would_send_if_approved": 12,
    "blocked_not_approved": 68,
    "blocked_rate_limit": 0,
    "blocked_missing_auth": 0
  }
}
```

### 4.11 `send --approved`

Current CLI/API send payloads are extended with approval evidence. A successful send returns:

```json
{
  "ok": true,
  "approved": true,
  "approval_id": "appr_20260510_000031",
  "account_id": 1,
  "send_id": 7,
  "platform_message_id": "urn:li:msg:123",
  "status": "sent",
  "was_duplicate": false,
  "idempotency_key": "linkedin-dm-2026-05-10-031",
  "sent_at": "2026-05-10T12:05:00Z"
}
```

If approval evidence is missing or mismatched, the command must exit non-zero and emit the error to stderr. Any JSON error envelope returned by the API must preserve the same safety signal:

```json
{
  "ok": false,
  "approved": false,
  "status": "rejected",
  "error": "approval_required",
  "external_writes": 0
}
```

## 5. UI screen map

### 5.1 Inbox and search

- **Purpose:** local-first triage of synced threads and messages.
- **Primary data:** `GET /ops/inbox`, `GET /ops/search`, `GET /threads`, `GET /threads/{thread_id}/messages`.
- **Search implementation note:** the shared storage layer currently uses a parameterized SQLite `LIKE` fallback over stored message text instead of FTS5. Responses include `fts: false` so the UI/CLI can surface that search is safe/local but not yet ranked full-text search.
- **Controls:** account selector, sync status badge, search box, date range, direction filter, thread list, unread/local-health filter.
- **Primary actions:** open thread, draft reply, copy redacted evidence snippet.
- **Empty state:** “No synced conversations yet” with a safe CTA to run sync. The CTA explains that sync reads from LinkedIn and does not send messages.
- **Loading state:** skeleton thread rows plus last successful local snapshot if available.
- **Error state:** redacted provider/API error with next action, such as refresh auth or reduce pagination.

### 5.2 Thread and contact detail

- **Purpose:** inspect a single conversation and the known contact identity before drafting.
- **Primary data:** `GET /ops/threads/{thread_id}`, `GET /ops/threads/{thread_id}/messages`, future local `contacts` view derived from thread participants/raw provider data.
- **Controls:** message timeline, copied profile URN, local notes, draft composer.
- **Primary actions:** draft reply, create campaign candidate, mark thread reviewed locally.
- **Empty state:** “Thread has no stored messages” with a sync CTA scoped to the account.
- **Loading state:** timeline shimmer with disabled draft action until thread metadata is loaded.
- **Error state:** local storage error or missing thread shown without raw cookies or provider headers.

### 5.3 Campaign and sync status

- **Purpose:** show progress and risk for sync jobs and campaign dry-runs.
- **Primary data:** `GET /ops/sync/status`, `GET /ops/campaigns/{campaign_id}/status`, `GET /sends`.
- **Controls:** account selector, campaign selector, dry-run button, sync button, pagination settings, rate-limit policy panel.
- **Primary actions:** run sync, run campaign dry-run, export validation artifact.
- **Empty state:** “No campaigns configured” with import/create guidance that stays local.
- **Loading state:** progress cards with pages fetched, threads touched, duplicates skipped, and rate-limit flag.
- **Error state:** auth expired, provider rate-limited, or local DB unavailable with safe remediation.

### 5.4 Draft and reply approval

- **Purpose:** convert operator-reviewed text into an explicit send approval.
- **Primary data:** `GET /ops/drafts`, `POST /ops/drafts`, `POST /ops/approvals/{approval_id}/approve`, `POST /ops/send-approved`.
- **Controls:** draft body, recipient, thread context, text hash, idempotency key, approval diff, approve button, send button.
- **Primary actions:** create draft, approve draft, revoke approval, send approved draft.
- **Empty state:** “No drafts awaiting approval.”
- **Loading state:** disabled approval/send buttons while the text hash and approval state are being checked.
- **Error state:** approval mismatch, stale draft, rate limit, auth expired, provider failure. The UI keeps the send button disabled until the mismatch is resolved.

### 5.5 Account health

- **Purpose:** make auth/session/proxy readiness visible before reads or writes.
- **Primary data:** `GET /auth/check`, `GET /ops/accounts/{account_id}/health`.
- **Controls:** account selector, refresh cookie guidance, bearer-auth status, proxy status if configured.
- **Primary actions:** run auth check, open local instructions for account refresh.
- **Empty state:** “No account configured” with a link to account creation docs.
- **Loading state:** checking auth badge and disabled mutation buttons.
- **Error state:** failed auth check with a redacted error and `POST /accounts/refresh` as remediation.

### 5.6 Audit and outbound history

- **Purpose:** prove what happened, when, and under which approval.
- **Primary data:** `GET /sends`, `GET /ops/audit`, `GET /ops/approvals`.
- **Controls:** filters for account, status, campaign, approval state, date range, idempotency key.
- **Primary actions:** export Objective 73 evidence, inspect failed send, copy redacted JSON.
- **Empty state:** “No outbound sends recorded.”
- **Loading state:** table skeleton with counters preserved from the last successful query.
- **Error state:** invalid status filter or storage unavailable, surfaced without raw provider payloads.

## 6. Shared API and storage contract

### 6.1 Existing endpoint contract to preserve

These endpoints already exist and remain the compatibility base:

- `GET /health`
- `POST /accounts`
- `POST /accounts/refresh`
- `GET /auth/check?account_id={account_id}`
- `GET /threads?account_id={account_id}`
- `POST /sync`
- `POST /sync/ingest`
- `POST /send`
- `GET /sends?account_id={account_id}&status={pending|sent|failed}`

The console and CLI must not remove or change these response fields. Additive fields are allowed when backward compatible.

### 6.2 Proposed endpoint names

The Ops Console uses `/ops/*` endpoints for local orchestration and keeps existing public endpoints intact.

- `GET /ops/status`
- `GET /ops/accounts/{account_id}/health`
- `POST /ops/auth/check`
- `POST /ops/sync/dry-run`
- `GET /ops/sync/status?account_id={account_id}`
- `GET /ops/inbox?account_id={account_id}&limit={limit}&cursor={cursor}`
- `GET /ops/search?account_id={account_id}&q={query}&from={date}&to={date}&direction={direction}&limit={limit}`
- `GET /ops/threads?account_id={account_id}&limit={limit}&cursor={cursor}`
- `GET /ops/threads/{thread_id}?account_id={account_id}`
- `GET /ops/threads/{thread_id}/messages?account_id={account_id}&limit={limit}&cursor={cursor}`
- `POST /ops/drafts`
- `GET /ops/drafts?account_id={account_id}&state={draft|approved|revoked|sent}`
- `POST /ops/approvals/{approval_id}/approve`
- `POST /ops/approvals/{approval_id}/revoke`
- `GET /ops/approvals?account_id={account_id}&state={state}`
- `GET /ops/campaigns/{campaign_id}/status`
- `POST /ops/campaigns/{campaign_id}/run-dry-run`
- `POST /ops/send-approved`
- `GET /ops/audit?account_id={account_id}&from={date}&to={date}`
- `GET /ops/validation/objective-73`

### 6.3 Endpoint mutation classification

| Endpoint | External LinkedIn read | External LinkedIn write | Local write | Default safety |
| --- | --- | --- | --- | --- |
| `GET /ops/status` | no | no | no | read-only |
| `GET /ops/accounts/{account_id}/health` | optional via auth check | no | no | read-only |
| `POST /ops/auth/check` | yes | no | no | read-only provider check |
| `POST /ops/sync/dry-run` | no | no | no | dry-run |
| `POST /sync` | yes | no | yes | read-only external, writes local cache |
| `POST /sync/ingest` | no | no | yes | local ingest from extension payload |
| `POST /ops/drafts` | no | no | yes | local draft only |
| `POST /ops/approvals/{approval_id}/approve` | no | no | yes | local approval only |
| `POST /ops/campaigns/{campaign_id}/run-dry-run` | no | no | yes | dry-run only |
| `POST /ops/send-approved` | no | yes | yes | requires approved state |
| `POST /send` | no | yes | yes | legacy direct send; console must wrap with approval |

### 6.4 Storage contract

Existing tables and primitives remain canonical:

- `accounts`: account auth, proxy, browser context, and timestamps.
- `threads`: one row per account/thread, keyed by `(account_id, platform_thread_id)`.
- `messages`: local archive of inbound and outbound messages with `direction in ('in', 'out')`.
- `sync_cursors`: per-account/thread pagination cursor.
- `outbound_sends`: idempotent send tracking with `pending`, `sent`, and `failed` statuses.

Ops Console additions should use additive migrations:

- `draft_replies`
  - `id`, `account_id`, `thread_id`, `recipient`, `text`, `text_sha256`, `campaign_id`, `idempotency_key`, `state`, `created_at`, `updated_at`.
  - Valid states: `draft`, `approved`, `revoked`, `sent`.
- `send_approvals`
  - `approval_id`, `draft_id`, `account_id`, `recipient`, `text_sha256`, `idempotency_key`, `state`, `approved_by`, `approved_at`, `revoked_at`.
  - Valid states: `draft`, `approved`, `revoked`, `used`.
- `campaigns`
  - `id`, `account_id`, `name`, `state`, `rate_limit_daily_cap`, `created_at`, `updated_at`.
  - Valid states: `draft`, `active`, `paused`, `archived`.
- `campaign_recipients`
  - `id`, `campaign_id`, `recipient`, `thread_id`, `draft_id`, `approval_id`, `state`, `last_error`, `created_at`, `updated_at`.
- `ops_audit_events`
  - `id`, `account_id`, `event_type`, `actor`, `entity_type`, `entity_id`, `redacted_payload_json`, `created_at`.

The `outbound_sends` table remains the provider execution ledger. Approval tables explain why a send was allowed; `outbound_sends` records what the provider attempted and returned.

### 6.5 Shared error envelope

For new `/ops/*` endpoints, errors should use this shape:

```json
{
  "ok": false,
  "error": {
    "code": "approval_required",
    "message": "An approved draft is required before sending.",
    "retryable": false,
    "redacted": true
  }
}
```

Legacy endpoints may keep FastAPI `detail` responses, but console adapters should normalize them before rendering.

## 7. Safety and approval gates

### 7.1 Read-only and dry-run defaults

- UI load, inbox, thread views, search, status, campaign status, and audit views are read-only.
- Campaign execution is dry-run only until a separate task implements approved campaign sends.
- Dry-run output must report `external_writes: 0`.
- Sync reads from LinkedIn but never sends LinkedIn messages.

### 7.2 Explicit approved-send state

A send is allowed only when all of these checks pass atomically:

1. `approval_id` exists.
2. Approval state is `approved`.
3. Approval account matches the requested `account_id`.
4. Approval recipient matches the requested `recipient` or draft recipient.
5. Approval `text_sha256` matches the exact outbound body.
6. Approval idempotency key matches the send request when one is supplied.
7. Approval has not been revoked or used.
8. Account health check is not known failed.
9. Rate-limit policy permits the send.

After a successful provider send, mark the approval `used`, mark the draft `sent`, and update `outbound_sends` to `sent`. If provider send fails after an allowed attempt, keep the approval state explicit and record the failure in `outbound_sends` plus `ops_audit_events`.

### 7.3 Rate-limit handling

- Respect the existing provider send interval and expose the next safe send time in status outputs.
- Treat LinkedIn 429, challenge, auth-expired, and Cloudflare-like blocks as safety events.
- Stop campaign execution on rate-limit signals and report `blocked_rate_limit` counts.
- Never retry sends in a tight loop. Retries require idempotency keys and backoff.

### 7.4 Redaction

- Reuse the existing redaction layer for logs and API responses.
- Redact `li_at`, `JSESSIONID`, CSRF tokens, proxy credentials, bearer tokens, cookies, raw browser headers, and provider request bodies that may contain secrets.
- Store validation artifacts with IDs, timestamps, status fields, and hashes rather than secrets.
- UI copy/export actions must use redacted JSON.

### 7.5 No unapproved external sends

- `send --approved` is the only CLI send path the Ops Console may expose.
- The console must not call legacy `POST /send` directly from a draft button. It must call `POST /ops/send-approved`, which performs the approval checks before delegating to the provider send path.
- If the approval lookup fails, no provider client is constructed and no external request is made.
- Bulk sends require each recipient/text pair to have its own approval record.

## 8. Validation checklist

Use this checklist for Objective 73 QA and downstream implementation tasks:

- `docs/ops-console-cli-spec.md` exists and is linked from `README.md`.
- CLI command tree covers status/auth, sync, inbox, search, threads/messages, draft-reply, campaign status/run `--dry-run`, and send `--approved`.
- JSON output schemas include success and safety-failure examples for send approval.
- UI screen map covers inbox/search, thread/contact detail, campaign/sync status, draft/reply approval, account health, audit/outbound history, and empty/error/loading states.
- API/storage contract lists existing endpoints, exact proposed `/ops/*` endpoint names, storage tables, and additive migrations.
- Safety gates document read-only/dry-run defaults, explicit approval state, rate-limit handling, redaction, and the ban on unapproved external sends.
- No section requires secret material in screenshots, logs, artifacts, or comments.
- `git diff --check` passes.

## 9. Evidence artifact format

Objective 73 validation evidence should be committed or attached as `validation/objective-73-ops-console-cli.md` using this structure:

```markdown
# Objective 73 Ops Console and CLI Validation

## Build
- Command: `uv run pytest`
- Result: pass
- Timestamp: 2026-05-10T12:00:00Z

## Static checks
- Command: `git diff --check`
- Result: pass

## CLI dry-run evidence
- Command: `python -m apps.cli status --db-path validation/local-test.sqlite`
- Redacted output file: `validation/artifacts/cli-status-redacted.json`

## Approval gate evidence
- Attempt without approval: rejected before provider send
- Attempt with matching approval: provider send path called once
- Attempt with stale text hash: rejected before provider send

## UI evidence
- Inbox/search screen: `validation/artifacts/inbox-search.png`
- Thread/contact detail screen: `validation/artifacts/thread-contact-detail.png`
- Campaign/sync status screen: `validation/artifacts/campaign-sync-status.png`
- Draft/reply approval screen: `validation/artifacts/draft-reply-approval.png`
- Account health screen: `validation/artifacts/account-health.png`
- Audit/outbound history screen: `validation/artifacts/audit-outbound-history.png`

## Notes
- Secrets present in raw environment: no
- External sends performed during validation: no, unless explicitly approved in the test fixture
```

The artifact must contain redacted output only. If real provider credentials are used for validation, the artifact records command names, IDs, statuses, and hashes, not cookies, tokens, or proxy URLs.
