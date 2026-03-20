# Desearch — LinkedIn DMs Sync

This repository is for building a **community-driven LinkedIn Direct Messages sync service**.

The goal: given a user’s **LinkedIn** session (typically browser cookies) and (optionally) a proxy, the service should be able to:

1. **Sync DM history** (fetch and store conversation history)
2. **Send DMs** to specific users

We’re intentionally keeping the first version minimal, so contributors can plug in better scraping/playwright strategies, storage backends, and deployment options.

## What we want to build (overview)

### Core capabilities

#### 1) Sync DM history
- Accept an authenticated **LinkedIn** session (typically **browser cookies**; optionally username/password if someone implements it safely)
- Optional **per-account proxy** (may be required depending on usage/location)
- Discover DM conversations/threads
- Fetch message history per conversation
- Persist messages in a normalized format (DB)
- Incremental sync (only fetch new messages after last checkpoint)

#### 2) Send DMs
- Send a DM to a specific recipient/profile
- Support idempotency / retries
- Record outbound message status

### Constraints / reality
- LinkedIn has strong anti-automation protections and frequent UI changes.
- Cookie-based sessions can expire and may trigger security challenges.
- Rate limiting, careful request patterns, and good operational hygiene are mandatory.

This repo is **NOT** about bypassing security challenges or breaking laws/terms. It’s about building a robust, opt-in syncing tool for accounts you own or have explicit permission to access.

## Non-goals
- Account takeover or credential harvesting
- Circumventing CAPTCHAs / 2FA / device challenges
- Mass spam / unsolicited messaging

## Proposed architecture

### Components
- **Worker**: does the actual sync/send actions for one account
- **API service**: manages accounts, schedules syncs, exposes endpoints
- **Storage**: database for accounts, conversations, messages, sync cursors

### Data model (suggested)
- `Account`: handle, cookies blob reference, proxy config, last sync time
- `Conversation`: conversation id, participants
- `Message`: message id, conversation id, sender id, text, media refs, timestamp
- `SyncCursor`: per conversation cursor/watermark for incremental sync

### Interfaces
- **Provider abstraction** (recommended):
  - `providers/linkedin/` implements LinkedIn-specific logic
  - Later we can add other providers as needed.

## MVP scope (what we want first)

1. A minimal Python service skeleton
2. A provider interface with placeholder LinkedIn implementation
3. A simple storage layer (SQLite first)
4. CLI commands:
   - `sync` (fetch conversations + messages)
   - `send` (send DM)

Contributors can then replace the provider implementation with:
- browser automation (Playwright)
- network scraping (session cookies + HTTP)
- official APIs (if and when possible)

## Repo layout (planned)

```
.
├─ apps/
│  └─ api/                 # FastAPI service
├─ libs/
│  ├─ core/                # shared models, storage, config
│  └─ providers/
│     └─ linkedin/         # LinkedIn provider (placeholder)
├─ scripts/
├─ tests/
└─ docs/
```

## Getting started (for contributors)

This repo uses **Python 3.11+** and a minimal dependency set.

### Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Run the API (FastAPI)
```bash
uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8899
```

### CLI (no web server)

Use the same SQLite database and provider stack as the API, without uvicorn:

```bash
python -m apps.cli sync --account-id 1
python -m apps.cli send --account-id 1 --recipient 'urn:li:fsd_profile:…' --text 'Hello'
```

Optional: `--db-path /path/to/desearch_linkedin_dms.sqlite` on each subcommand. Sync also accepts `--limit-per-thread`, `--max-pages-per-thread`, and `--exhaust-pagination` (same semantics as `POST /sync`). Send accepts `--idempotency-key`.

If the provider raises `NotImplementedError` (for example sync before thread listing is implemented), the CLI prints a short TODO pointing at `libs/providers/linkedin/provider.py` and exits with a non-zero status.

Open:
- Health: http://127.0.0.1:8899/health
- Swagger UI: http://127.0.0.1:8899/docs

### Quick test with curl (no real cookies)
> This only verifies the API + SQLite wiring. Provider methods are still TODO.

1) Create an account (DO NOT use real cookies in public logs)
```bash
curl -s -X POST http://127.0.0.1:8899/accounts \
  -H 'Content-Type: application/json' \
  -d '{"label":"test","li_at":"REDACTED","jsessionid":null,"proxy_url":null}'
```

2) List threads (will be empty until provider is implemented)
```bash
curl -s 'http://127.0.0.1:8899/threads?account_id=1'
```

3) Trigger sync (currently returns a note until provider is implemented)
```bash
curl -s -X POST http://127.0.0.1:8899/sync \
  -H 'Content-Type: application/json' \
  -d '{"account_id":1,"limit_per_thread":50}'
```

## LinkedIn cookies

This service currently accepts LinkedIn session cookies for account authentication.

### Required
- `li_at`: the primary LinkedIn session cookie

### Optional
- `JSESSIONID`: may be needed later for provider requests that require CSRF-related headers

### Notes
- Treat both values as secrets
- Do not commit them into git
- Do not paste real cookie values into public issues, logs, or screenshots

### Example account creation
```bash
curl -s -X POST http://127.0.0.1:8899/accounts \
  -H 'Content-Type: application/json' \
  -d '{"label":"test","li_at":"REDACTED","jsessionid":"REDACTED","proxy_url":null}'
```
  
### Verify session

After creating an account, you can quickly verify that the stored cookies look valid using the auth check endpoint.

```bash
curl -s 'http://127.0.0.1:8899/auth/check?account_id=1'
```

### Example success response:
```json
{
  "status": "ok",
  "error": null
}
```

### Important note: SQLite + FastAPI threads
FastAPI runs normal `def` endpoints inside a threadpool. SQLite connections are thread-bound by default.

For MVP simplicity we open the connection with `check_same_thread=False`.
If you later add concurrency/background workers, consider using one connection per request or a pool.

## How to contribute

- Pick an issue and comment that you’re working on it.
- Keep PRs small and focused.
- Add tests where possible.

## Security & privacy

Cookies and session tokens are extremely sensitive.

**Do not** commit real cookies or credentials.

When implementing account auth handling:
- Encrypt cookies at rest
- Support secret managers via env vars
- Add redaction in logs

## Safe logging rules

The service uses a **defense-in-depth** approach to prevent secrets from leaking
into logs, HTTP responses, or tracebacks. Three layers work together:

### Layer 1 — Source-level (`__repr__` overrides)

`AccountAuth`, `ProxyConfig`, and `LinkedInProvider` override `__repr__` and
`__str__` so secrets are never exposed through `print()`, f-strings, tracebacks,
or any other string conversion — even without the logging filter.

```python
>>> repr(AccountAuth(li_at="secret", jsessionid="ajax:tok"))
"AccountAuth(li_at='[REDACTED]', jsessionid='[REDACTED]')"
```

### Layer 2 — Filter-level (`SecretRedactingFilter`)

`configure_logging()` (called automatically in `apps/api/main.py`) installs a
`SecretRedactingFilter` on the root logger. Every log record passes through it
before being emitted — no manual opt-in required per call site. The filter scrubs:

- **Message strings** via `redact_string()` (inline patterns)
- **Structured args** (dicts) via `redact_for_log()`
- **Dataclass args** (e.g. `AccountAuth`) via `dataclasses.asdict()` + redaction
- **Exception tracebacks** (`exc_text` and `exc_info`) to catch secrets in stack traces

### Layer 3 — API-level (HTTP response sanitization)

All `HTTPException` detail strings in `apps/api/main.py` are passed through
`redact_string()` before being returned to clients, preventing secrets from
leaking through error responses.

### Redacted keys (structured data)

When logging dicts or request bodies, wrap them with `redact_for_log()`:

```python
from libs.core.redaction import redact_for_log

logger.info("Account created: %s", redact_for_log({"account_id": 1, "li_at": "SECRET"}))
# → Account created: {'account_id': 1, 'li_at': '[REDACTED]'}
```

The following dict keys are always redacted (case-insensitive):
`li_at`, `jsessionid`, `auth_json`, `cookie`, `cookies`, `authorization`,
`password`, `secret`, `token`, `api_key`, `apikey`, `proxy_url`, `url`

### Redacted patterns (inline strings)

Inline secrets in log message strings are scrubbed by `redact_string()` and
automatically by the logging filter. Examples of patterns that get redacted:

```
li_at=SECRETVALUE                →  li_at=[REDACTED]
JSESSIONID: ajax:csrf123         →  JSESSIONID: [REDACTED]
Authorization: Bearer eyJhbGc    →  Authorization: [REDACTED]
Authorization=Basic dXNlcjpw     →  Authorization=[REDACTED]
password=hunter2                 →  password=[REDACTED]
proxy_url=http://u:p@host:8080   →  proxy_url=[REDACTED]
```

### Rules for contributors

1. **Never log raw `AccountAuth` objects** — always pass through `redact_for_log()` first.
2. **Never log raw cookie strings** — use `redact_string()` or rely on the filter.
3. **Never log request bodies verbatim** — extract only the non-sensitive fields.
4. **Do not disable the logging filter** — `configure_logging()` must remain in `main.py`.
5. **Do not add `li_at` / `jsessionid` to error messages** — use account_id instead.
6. **Always override `__repr__`** on any new dataclass that holds secrets.

## Roadmap

- [ ] MVP skeleton: FastAPI + SQLite + provider interface
- [ ] LinkedIn provider: conversation discovery + incremental sync (TBD)
- [ ] LinkedIn provider: send DM (TBD)
- [ ] Proxy + per-account rate limiting

---

If you want to help, start with the issues in this repo.