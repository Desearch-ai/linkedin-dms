# Known issues and operational caveats

This file documents issues that are real in the current implementation, based on the source code in `apps/api/main.py`, `libs/core/`, and `libs/providers/linkedin/provider.py`.

## 1. GraphQL query IDs can go stale

`LinkedInProvider.list_threads()` and `LinkedInProvider.fetch_messages()` depend on hardcoded Voyager GraphQL query IDs:
- `messengerConversations.0d5e6781bbee71c3e51c8843c6519f48`
- `messengerMessages.21eabeb3ee872254060ef21b793ea7d0`

These values are extracted from LinkedIn frontend traffic and may change without warning. When they change, thread listing or message fetch can fail even though cookies are still valid.

Impact:
- sync can return 4xx or fail during provider execution
- the service may appear broken until the query IDs are updated in source

## 2. `JSESSIONID` is effectively required for sync

The code allows account creation with only `li_at`, but the GraphQL header builder requires a non-empty `JSESSIONID` for Voyager requests.

Impact:
- account creation succeeds with only `li_at`
- `GET /auth/check` may still report success if `li_at` is present
- later sync operations can fail because thread listing and message fetch need `JSESSIONID`

Operational advice:
- provide both `li_at` and `JSESSIONID` for any account that should sync threads or messages

## 3. `/auth/check` is only a local sanity check

`LinkedInProvider.check_auth()` currently validates cookie presence and basic formatting only. It does not make a live LinkedIn request.

Impact:
- `status: ok` does not prove the cookies are accepted by LinkedIn
- expired, challenged, or region-blocked sessions may still pass `/auth/check`

## 4. Cloudflare and bot defenses remain a moving target

The provider contains fallback logic to harvest browser cookies with Playwright when GraphQL requests appear blocked. That helps, but it is not a guaranteed fix.

Why it is fragile:
- Playwright is optional and may not be installed
- browser navigation to the messaging page can still fail
- Cloudflare behavior can vary by IP, geography, session age, and proxy quality
- LinkedIn can change challenge behavior at any time

Impact:
- sync reliability can vary substantially across environments

## 5. Sync is synchronous and request-bound

The FastAPI `POST /sync` endpoint performs the full sync inline inside the request lifecycle.

Impact:
- long syncs keep the request open
- retries and backoff can make the request last a long time
- there is no built-in queue, scheduler, worker pool, or cancellation path

## 6. SQLite is simple but not a multi-worker architecture

The current storage layer is intentionally lightweight and uses one SQLite file with a process-wide connection.

Impact:
- this is suitable for local development and small-scale use
- it is not designed for horizontally scaled workers or high write concurrency
- long-running or overlapping sync operations may still contend on the same file

## 7. Plaintext storage is allowed when encryption is not configured

If `DESEARCH_ENCRYPTION_KEY` is missing, auth and proxy JSON are stored in plaintext. The code logs a warning once, but it does not block startup.

Impact:
- the system remains easy to start in development
- local database files may contain raw `li_at`, `JSESSIONID`, and proxy values

Operational advice:
- set `DESEARCH_ENCRYPTION_KEY` anywhere secrets at rest matter

## 8. Send idempotency is durable, but only by key

`run_send()` uses the `outbound_sends` table to enforce idempotency when an `idempotency_key` is provided.

Impact:
- repeated sends with the same key and same payload are safely deduplicated
- calls without an idempotency key always create a new pending send row
- reusing a key with different recipient or text raises an error

This is intended behavior, but it means callers must supply keys consistently if they want duplicate protection across retries.

## 9. Outbound message threading is simplified

After a successful send, `run_send()` archives the sent message by upserting a thread whose `platform_thread_id` is the provided `recipient` value.

Impact:
- if `recipient` is a profile URN instead of a real conversation URN, local thread history for sent messages may not align perfectly with sync-discovered conversation identifiers
- this is acceptable for the current MVP, but it is not a full conversation reconciliation model

## 10. Message pagination cursor is heuristic

`fetch_messages()` computes `next_cursor` from the oldest fetched message timestamp when the returned element count reaches the requested limit.

Impact:
- pagination depends on LinkedIn continuing to interpret `createdBefore` in the expected way
- if response ordering or cursor semantics change, paging could skip or repeat messages

## 11. API error sanitization is good, but not universal by design

Many API paths redact exception detail strings before returning them, and logging has a global redaction filter. That said, callers should still avoid building exception messages that embed secrets.

Impact:
- the code is defensive, not magical
- future changes can still introduce leak risks if contributors bypass redaction helpers

## 12. `POST /send` returns raw conflict text for some failures

In `apps/api/main.py`, some `ValueError` and `RuntimeError` exceptions from send flow are returned directly as 409 details.

Impact:
- current messages appear safe because send flow errors do not include cookie material
- future changes should keep that invariant in mind

## 13. Provider read and write paths do not share identical auth behavior

GraphQL read operations require profile discovery and GraphQL-specific headers, while send uses the messaging conversations endpoint and a different request path.

Impact:
- an account may succeed on one path and fail on the other depending on session state, cookies, or upstream behavior
- debugging should treat sync failures and send failures as related but not identical problems

## 14. Playwright cookie harvesting assumes Chromium availability

The fallback browser path launches Chromium through Playwright.

Impact:
- environments without the browser installed cannot use this fallback
- headless browser restrictions, sandbox differences, or proxy incompatibilities may break the flow

## 15. The repo still contains an older high-level overview

`docs/PROJECT_OVERVIEW.md` is still present alongside the updated docs.

Impact:
- contributors should prefer `README.md`, `docs/features.md`, and `docs/architecture.md` for the current implementation picture
- the overview file may describe the project at a higher and older level than the code now reflects

## 16. CLI help text can be read as more permissive than the effective default

In `apps/cli/__main__.py`, `--max-pages-per-thread` is declared with `default=None`, but the parser later resolves the effective default to `1` page unless `--exhaust-pagination` is set.

Impact:
- the runtime behavior is correct and matches the API MVP default
- contributors reading only the argparse declaration can misread the default behavior
- docs should call out the effective one-page default explicitly, which this docs pass now does

## 17. API auth is optional, not mandatory

The local API can now require a bearer token through `DESEARCH_API_TOKEN`, but the protection is opt-in.

Impact:
- local setups that do not set the token still expose account, sync, send, and thread routes to any local process that can reach the port
- this keeps zero-config local development simple, but it is not a hardened default for shared or remotely exposed environments

Operational advice:
- keep the bind address at `127.0.0.1`
- set `DESEARCH_API_TOKEN` before using any non-localhost binding, reverse proxy, or shared workstation setup
