# Desearch LinkedIn DMs

LinkedIn DMs is a Python service for storing LinkedIn messaging data in SQLite and exposing it through a small FastAPI API and CLI.

The current repository is no longer just a skeleton. It already includes:
- account creation and cookie refresh endpoints
- SQLite migrations and persistence for accounts, threads, messages, cursors, and outbound sends
- a LinkedIn provider that can list threads, fetch messages, send messages, and perform a lightweight auth check
- log and error redaction for sensitive fields such as `li_at`, `JSESSIONID`, proxy URLs, and tokens

What is still true is that LinkedIn is a moving target. Some parts are implemented against private Voyager and GraphQL endpoints, so reliability depends on cookie validity, current LinkedIn query IDs, anti-bot responses, and optional Playwright cookie harvesting.

## Repository layout

```text
.
├─ apps/
│  ├─ api/                 # FastAPI application
│  └─ cli/                 # CLI entrypoint for sync/send without uvicorn
├─ libs/
│  ├─ core/                # models, storage, crypto, cookie parsing, redaction, job orchestration
│  └─ providers/
│     └─ linkedin/         # LinkedIn-specific HTTP + Playwright-assisted provider
├─ docs/
│  ├─ architecture.md
│  ├─ features.md
│  ├─ known-issues.md
│  └─ ops-console-cli-spec.md
├─ scripts/
└─ tests/
```

## Ops Console and CLI spec

The repo-backed Objective 73 specification for the safe operator console, CLI command tree, proposed `/ops/*` API surface, approval gates, and validation evidence format lives in [`docs/ops-console-cli-spec.md`](docs/ops-console-cli-spec.md).

## Requirements

- Python 3.11+
- SQLite, stored in `./desearch_linkedin_dms.sqlite` by default
- `li_at` cookie for every account
- `JSESSIONID` for Voyager/GraphQL endpoints used by thread listing and message fetch
- optional Playwright when LinkedIn or Cloudflare blocks cookie-only GraphQL access

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Optional browser support for Cloudflare cookie harvesting:

```bash
pip install -e '.[browser]'
playwright install chromium
```

Optional at-rest encryption for stored auth and proxy payloads:

```bash
export DESEARCH_ENCRYPTION_KEY="$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")"
```

If `DESEARCH_ENCRYPTION_KEY` is not set, the app still works, but auth and proxy JSON are stored in plaintext and the process logs a one-time warning.

Optional local API bearer auth:

```bash
export DESEARCH_API_TOKEN='replace-with-a-local-shared-secret'
```

If `DESEARCH_API_TOKEN` is set, all routes except `GET /health` require:

```bash
Authorization: Bearer <token>
```

## Running the API

```bash
uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8899
```

Useful endpoints:
- `GET /health`
- `POST /accounts`
- `POST /accounts/refresh`
- `GET /auth/check?account_id=1`
- `GET /threads?account_id=1`
- `POST /sync`
- `POST /send`
- `GET /sends?account_id=1`

Swagger UI is available at <http://127.0.0.1:8899/docs>.

Security posture for local development:
- bind to `127.0.0.1`, not `0.0.0.0`
- set `DESEARCH_API_TOKEN` if other local processes should not be able to drive the API
- configure the same bearer token in the Chrome extension popup when API auth is enabled

## Chrome extension preflight

Before validating the extension-button sync path, run the local API and launch or
verify Chrome through the deterministic preflight harness:

```bash
uvicorn apps.api.main:app --host 127.0.0.1 --port 8899
uv run python scripts/extension_preflight.py --launch
```

The preflight checks `GET /health` at `http://127.0.0.1:8899`, starts or verifies
Chrome on CDP port `18800`, loads the unpacked extension from
`chrome-extension/`, and requires executable runtime proof for the expected
extension id. A matching `chrome-extension://...` URL alone is not enough: the
harness must see the MV3 `background.js` service-worker target or evaluate the
popup runtime and confirm `chrome.runtime.id` plus the manifest identity. It then
opens <https://www.linkedin.com/messaging/> so the extension can capture the
current messaging contract before `Sync Now`.

Useful knobs:

```bash
uv run python scripts/extension_preflight.py --help
uv run python scripts/extension_preflight.py \
  --backend-url http://127.0.0.1:8899 \
  --cdp-port 18800 \
  --profile-dir ~/.desearch-linkedin-dms/chrome-profile \
  --extension-path ./chrome-extension \
  --launch
```

Use a Chrome profile that is signed in to LinkedIn for live validation. If you
use a custom backend URL or API token, set the matching service URL/token in the
extension popup before pressing `Refresh` or `Sync Now`. The harness prints only
redacted operational status; it must not be used to dump LinkedIn session
secrets, browser CSRF material, or local API auth values.


### API request and response shape

`GET /health`

```json
{"ok": true}
```

`POST /accounts`

```json
{
  "label": "sales-1",
  "cookies": "li_at=REDACTED; JSESSIONID=ajax:REDACTED",
  "proxy_url": "http://user:pass@proxy.example:8080"
}
```

```json
{"account_id": 1}
```

`POST /sync`

```json
{
  "account_id": 1,
  "limit_per_thread": 50,
  "max_pages_per_thread": 1,
  "delay_between_threads_s": 2.0,
  "delay_between_pages_s": 1.5
}
```

```json
{
  "ok": true,
  "synced_threads": 12,
  "messages_inserted": 84,
  "messages_skipped_duplicate": 31,
  "pages_fetched": 14,
  "rate_limited": false
}
```

`POST /send`

```json
{
  "account_id": 1,
  "recipient": "urn:li:fsd_profile:123",
  "text": "Hello",
  "idempotency_key": "linkedin-dm-2026-04-09-001"
}
```

```json
{
  "ok": true,
  "send_id": 7,
  "platform_message_id": "urn:li:msg:123",
  "status": "sent",
  "was_duplicate": false
}
```

## Running the CLI

The CLI uses the same SQLite storage and provider stack as the API, but is safe-by-default for operator workflows. Browsing, search, status, drafts, and campaign dry-runs are local-only. Live sends are approval-gated and refuse to run unless an approved local approval record matches the exact account, recipient, text hash, and idempotency key.

```bash
# Local status and account readiness
python -m apps.cli status --db-path ./desearch_linkedin_dms.sqlite
python -m apps.cli auth status --account-id 1

# Sync reads LinkedIn and writes local cache; pagination defaults to one page per thread
python -m apps.cli sync --account-id 1 --limit-per-thread 50
python -m apps.cli sync --account-id 1 --dry-run

# Local inbox/search/detail inspection
python -m apps.cli inbox --account-id 1 --limit 25 --json
python -m apps.cli search --account-id 1 --query bittensor --direction in --json
python -m apps.cli threads list --account-id 1 --limit 25 --json
python -m apps.cli threads show --account-id 1 --thread-id 10 --include-messages --json
python -m apps.cli messages list --account-id 1 --thread-id 10 --json

# Local draft only; this does not send to LinkedIn
python -m apps.cli draft-reply --account-id 1 --recipient 'urn:li:fsd_profile:123' --text 'Hello' --idempotency-key linkedin-dm-2026-05-10-001

# Campaign execution is dry-run only in this phase
python -m apps.cli campaign status --campaign-id 5 --account-id 1
python -m apps.cli campaign run --campaign-id 5 --account-id 1 --dry-run

# Live send requires explicit approved evidence; text alone is rejected
python -m apps.cli send --approved appr_20260510_000031 --account-id 1 --recipient 'urn:li:fsd_profile:123' --text 'Hello' --idempotency-key linkedin-dm-2026-05-10-001
```

Useful sync options:
- `--db-path PATH`
- `--limit-per-thread N`
- `--max-pages-per-thread N`
- `--exhaust-pagination`
- `--delay-threads SEC`
- `--delay-pages SEC`
- `--dry-run`

CLI pagination behavior matches the API:
- default effective behavior is one page per thread
- `--max-pages-per-thread N` sets an explicit cap
- `--exhaust-pagination` follows cursors until exhaustion

Useful read/detail options:
- `--json` for machine-readable output (the CLI emits JSON on success by default)
- `--limit N` and `--cursor CURSOR` for local pagination
- `--unread`/`--unread-only` on inbox (currently no local unread state is tracked)
- `--from`, `--to`, and `--direction in|out` on search

Useful send options:
- `--approved APPROVAL_ID` (required for live sends)
- `--draft-id ID` to source recipient/body from a stored draft
- `--idempotency-key KEY`

## Account authentication input

`POST /accounts` and `POST /accounts/refresh` accept either:
- explicit `li_at` and optional `jsessionid`
- a `cookies` field containing either a raw cookie header string or a JSON cookie export

Examples:

```bash
curl -s -X POST http://127.0.0.1:8899/accounts \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer replace-with-a-local-shared-secret' \
  -d '{"label":"sales-1","li_at":"REDACTED","jsessionid":"ajax:REDACTED"}'
```

```bash
curl -s -X POST http://127.0.0.1:8899/accounts \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer replace-with-a-local-shared-secret' \
  -d '{"label":"sales-1","cookies":"li_at=REDACTED; JSESSIONID=ajax:REDACTED"}'
```

Refresh an existing account without recreating it:

```bash
curl -s -X POST http://127.0.0.1:8899/accounts/refresh \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer replace-with-a-local-shared-secret' \
  -d '{"account_id":1,"cookies":"li_at=REDACTED; JSESSIONID=ajax:REDACTED"}'
```

Quick auth sanity check:

```bash
curl -s 'http://127.0.0.1:8899/auth/check?account_id=1'
```

## Sync behavior

`POST /sync` and `python -m apps.cli sync` both call `libs.core.job_runner.run_sync()`.

Current behavior:
- loads account auth and optional proxy from storage
- calls `LinkedInProvider.list_threads()`
- upserts each thread into SQLite
- fetches messages page by page with cursor support
- inserts only new messages, counting duplicate skips separately
- stores the latest cursor in `sync_cursors`
- sleeps between threads and pages to reduce rate-limit pressure
- returns summary counts including `rate_limited`

Default API sync payload:

```json
{
  "account_id": 1,
  "limit_per_thread": 50,
  "max_pages_per_thread": 1,
  "delay_between_threads_s": 2.0,
  "delay_between_pages_s": 1.5
}
```

Set `max_pages_per_thread` to `null` in the API or pass `--exhaust-pagination` in the CLI to keep following cursors until exhaustion.

## Send behavior

`POST /send`, `/ops/send-approved`, and the CLI ultimately call `libs.core.job_runner.run_send()`, but the operator CLI only exposes the approval-gated path.

Current behavior:
- creates or reuses an outbound send record before calling LinkedIn
- enforces idempotency through the `outbound_sends` table when a key is provided
- retries transient network errors and backs off on rate limiting
- stores successful outbound messages in both `outbound_sends` and `messages`
- exposes historical send records through `GET /sends`
- requires `python -m apps.cli send --approved APPROVAL_ID ...`; without approved state the CLI exits non-zero before loading the provider

## Storage summary

The SQLite database currently contains these tables:
- `accounts`
- `threads`
- `messages`
- `sync_cursors`
- `schema_version`
- `outbound_sends`
- `draft_replies`
- `send_approvals`
- `campaigns`
- `campaign_recipients`
- `ops_audit_events`

Migrations also add message direction constraints, indexes, the `outbound_sends(account_id, status)` lookup path used by `GET /sends`, and local Ops Console tables for drafts, approvals, campaign dry-run state, and redacted audit evidence.

## Security notes

The codebase already includes several concrete protections:
- `AccountAuth`, `ProxyConfig`, and `LinkedInProvider` redact their own string representations
- `configure_logging()` installs `SecretRedactingFilter` on the root logger
- `redact_string()` and `redact_for_log()` sanitize logs, dict payloads, and exception text
- API validation and `HTTPException` detail strings pass through redaction helpers before returning to clients
- optional Fernet encryption protects stored auth and proxy JSON at rest

Even with those safeguards:
- do not commit real cookies
- do not paste real cookies into issue trackers or logs
- treat `li_at`, `JSESSIONID`, proxy URLs, and any exported cookie bundle as secrets

## What to read next

- `docs/features.md` for implementation status by feature
- `docs/architecture.md` for component and request flow details
- `docs/known-issues.md` for sharp edges and operational caveats
