# Architecture

## Overview

The repository is organized as a small Python application with three core layers:
- `apps/api` exposes FastAPI endpoints
- `apps/cli` exposes sync and send commands without running a web server
- `libs/core` and `libs/providers/linkedin` contain the actual behavior shared by both entrypoints

Both the API and CLI are thin wrappers. Most real work happens in `Storage`, `run_sync()`, `run_send()`, and `LinkedInProvider`.

## Main components

### 1. FastAPI application (`apps/api/main.py`)

Responsibilities:
- bootstraps logging redaction with `configure_logging()`
- creates a process-wide `Storage` instance
- runs `storage.migrate()` at import time
- defines request models for account creation, refresh, sync, and send
- converts request payloads into `AccountAuth` and `ProxyConfig`
- maps storage or provider errors into HTTP responses

Important design detail:
- the module creates a singleton SQLite-backed `Storage` object at startup, not per request
- the SQLite connection is opened with `check_same_thread=False` because FastAPI executes sync endpoints in a threadpool

### 2. CLI entrypoint (`apps/cli/__main__.py`)

Responsibilities:
- parses arguments for `sync` and `send`
- opens storage and runs migrations
- loads account auth and proxy for the selected account
- invokes the same job runner functions as the API
- prints JSON on success and human-readable errors on failure

The CLI deliberately shares the provider and job runner path with the API so behavior stays aligned.

### 3. Core models (`libs/core/models.py`)

The data layer uses lightweight dataclasses:
- `AccountAuth`
- `ProxyConfig`
- `Account`
- `Thread`
- `Message`

`AccountAuth` and `ProxyConfig` override `__repr__` and `__str__` so secrets are not exposed if an object is accidentally logged.

### 4. Cookie parsing (`libs/core/cookies.py`)

This module normalizes auth input into `AccountAuth`.

Supported input shapes:
- raw cookie header string
- JSON array of browser-exported cookies

The API request models call this code when a `cookies` field is provided.

### 5. Encryption layer (`libs/core/crypto.py`)

Storage does not directly decide whether auth is encrypted. Instead it calls:
- `encrypt_if_configured()` before writing `auth_json` and `proxy_json`
- `decrypt_if_encrypted()` after reading them

Behavior:
- if `DESEARCH_ENCRYPTION_KEY` is set to a valid Fernet key, stored auth and proxy blobs are encrypted
- if the key is missing, values are stored as plaintext and a one-time warning is logged
- decrypt reads remain backward-compatible with legacy plaintext rows

### 6. Storage (`libs/core/storage.py`)

`Storage` is the persistence boundary and migration manager.

Current schema:
- `accounts`
- `threads`
- `messages`
- `sync_cursors`
- `schema_version`
- `outbound_sends`

Schema versioning today:
- version `0`: baseline tables
- version `1`: thread/message indexes
- version `2`: `messages.direction` check constraint (`in` or `out`)
- version `3`: `outbound_sends` table and account/status index

Key behaviors:
- initializes SQLite in WAL mode with foreign keys enabled
- creates baseline tables on first run
- applies incremental migrations for indexes, stricter message constraints, and outbound send tracking
- stores auth and proxy JSON in encrypted or plaintext form depending on configuration
- normalizes message timestamps to UTC ISO strings
- uses uniqueness constraints to prevent duplicate thread and message rows

### 7. Job runner (`libs/core/job_runner.py`)

The job runner coordinates persistence with provider calls.

#### `run_sync()`
Responsibilities:
- call `provider.list_threads()`
- upsert each thread into local storage
- read the saved cursor for each thread
- call `provider.fetch_messages()` page by page
- insert messages, counting duplicates separately
- write the new cursor after each page
- sleep between threads and between pages according to `SyncConfig`
- return a `SyncResult` summary

#### `run_send()`
Responsibilities:
- create or reuse an outbound send record before contacting LinkedIn
- enforce durable idempotency using `outbound_sends`
- block duplicate in-flight sends with `status='pending'`
- mark the send as failed or sent after the provider call
- archive successful outbound messages into `messages`
- return a `SendResult`

This design means idempotency does not depend on process memory alone.

### 8. LinkedIn provider (`libs/providers/linkedin/provider.py`)

This is the implementation boundary for LinkedIn-specific network behavior.

Capabilities:
- `check_auth()` for lightweight local validation
- `list_threads()` via Voyager GraphQL
- `fetch_messages()` via Voyager GraphQL
- `send_message()` via the messaging conversations endpoint

Important internal state:
- `_client` caches an `httpx.Client` for GraphQL GET requests
- `_browser_cookies` optionally caches cookies harvested through Playwright
- `_profile_id` caches the account identity fetched from `/voyager/api/me`
- `rate_limit_encountered` records whether the provider hit 429 or 999 responses

## Request and data flow

### Account creation flow

1. Client calls `POST /accounts`.
2. `AccountCreateIn` validates that either `cookies` or `li_at` exists.
3. The request model converts input into `AccountAuth`.
4. Optional `proxy_url` becomes `ProxyConfig`.
5. `Storage.create_account()` writes the row to SQLite, encrypting auth/proxy JSON when configured.
6. The API logs a redacted account creation event and returns `account_id`.

### Sync flow

1. Client calls `POST /sync` or runs `python -m apps.cli sync`.
2. The entrypoint loads auth and proxy from `Storage`.
3. A `LinkedInProvider` is created with that account context.
4. `run_sync()` calls `provider.list_threads()`.
5. `list_threads()` fetches the account profile from `/voyager/api/me` to build the mailbox URN.
6. The provider calls the GraphQL conversations endpoint page by page using the current sync token.
7. Each thread is upserted into SQLite.
8. For each thread, `run_sync()` loads the last saved cursor and calls `provider.fetch_messages()`.
9. Parsed messages are inserted into `messages`; duplicates are skipped by constraint.
10. The cursor is updated in `sync_cursors`.
11. The caller receives aggregate counts.

### Send flow

1. Client calls `POST /send` or runs `python -m apps.cli send`.
2. The entrypoint loads auth and proxy from `Storage`.
3. `run_send()` creates or reuses an `outbound_sends` row.
4. If the idempotency key matches a prior successful send with the same payload, the cached result is returned.
5. Otherwise `provider.send_message()` posts a conversation-create payload to LinkedIn.
6. On success, `Storage.mark_outbound_sent()` persists the provider message ID.
7. The sent message is also inserted into `messages` using the recipient identifier as the local thread key.
8. The caller receives the send record ID and platform message ID.

## Network strategy

### GraphQL reads

Thread listing and message fetch use GET requests against Voyager GraphQL endpoints with hardcoded query IDs.

Headers include:
- browser-like user agent
- `x-restli-protocol-version`
- `x-li-track`
- `x-li-page-instance`
- `csrf-token` derived from `JSESSIONID`

If GraphQL appears blocked by Cloudflare or a redirect/HTML challenge, the provider can switch to Playwright-assisted cookie harvesting:
- launch Chromium
- inject `li_at` and `JSESSIONID`
- navigate to LinkedIn messaging
- collect the resulting browser cookie jar
- retry the GraphQL request with those cookies

### Send writes

Sending uses a separate POST request path to the messaging conversations endpoint with:
- JSON payload containing `MessageCreate`
- browser-like headers
- the same account cookies
- retry and backoff logic for network and rate-limit failures

The persistence path is deliberately durable:
- `create_or_get_outbound_send()` inserts a pending row before the network call
- `mark_outbound_sent()` or `mark_outbound_failed()` records the terminal state afterward
- successful sends are mirrored into `messages` so local history includes outbound content, even though the thread key is currently simplified to the provided recipient identifier

## Retry and backoff model

The provider distinguishes several failure classes:
- network errors and timeouts
- retryable 5xx responses
- LinkedIn rate limiting via 429 or 999
- auth failures via 401 and some 403 cases

Behavior differs slightly between GET and POST paths, but both include bounded retries and sleeps. Sync also sleeps between threads and pages through `SyncConfig` to reduce request pressure.

## Redaction and secret handling

Secrets can enter the system through cookies, proxy URLs, and auth-related exceptions. The code attempts to defend at multiple layers:

1. object-level redaction in `__repr__`
2. structured and string redaction helpers
3. a global logging filter attached by `configure_logging()`
4. redaction of several API error details before returning them to clients
5. optional encryption of stored auth/proxy blobs

## Current architectural constraints

- SQLite is local-file storage only
- the API keeps one process-wide `Storage` object and one underlying connection
- sync work is synchronous, not queued or backgrounded
- LinkedIn GraphQL query IDs are embedded in source and may require manual updates
- GraphQL reads depend on `JSESSIONID`; send auth can work with the basic cookie set used by `send_message()`
- some browser-assisted resilience depends on optional Playwright installation and LinkedIn page behavior

## Extension points

The most natural extension points in the current code are:
- `LinkedInProvider` for new request strategies or safer parsing
- `Storage` for new tables or alternative persistence backends
- `run_sync()` for scheduling, batching, or richer sync policies
- `run_send()` for richer outbound status handling
- API request models and endpoints for account management or operational controls
