# Feature status

This file reflects the code that exists today in `apps/api/main.py`, `libs/core/`, and `libs/providers/linkedin/provider.py`.

Status legend:
- ✅ working
- ⚠️ degraded
- ❌ broken
- 🚧 in progress

## API surface

- ✅ `GET /health`
  - Returns `{ "ok": true }`.

- ✅ `POST /accounts`
  - Creates an account row in SQLite.
  - Accepts either `li_at` or a `cookies` payload.
  - Supports optional `proxy_url`.
  - Validates that `li_at` is present and not obviously malformed.

- ✅ `POST /accounts/refresh`
  - Replaces stored auth for an existing account.
  - Returns 404 when the account does not exist.

- ✅ `GET /auth/check`
  - Performs a lightweight local auth sanity check through `LinkedInProvider.check_auth()`.
  - Currently verifies cookie presence and basic formatting only.
  - Does not make a live LinkedIn request.

- ✅ `GET /threads`
  - Returns stored threads for a given account from SQLite.
  - This is a storage read endpoint, not a live LinkedIn fetch.

- ✅ `POST /sync`
  - Runs the full sync orchestration path.
  - Loads account auth and proxy from storage.
  - Calls provider thread listing and message fetch.
  - Persists threads, messages, and cursors.
  - Returns counts for synced threads, inserted messages, duplicate skips, fetched pages, and whether rate limiting was encountered.
  - Returns `401` with the existing refresh hint when LinkedIn rejects `/voyager/api/me` bootstrap as an auth/session failure.
  - Returns `422` with a bootstrap-specific refresh hint when `/voyager/api/me` returns blocked HTML or another unusable non-auth payload.

- ✅ `POST /send`
  - Sends a message through the provider.
  - Uses durable idempotency tracking in `outbound_sends`.
  - Stores successful outbound messages in the local message archive.

- ✅ `GET /sends`
  - Returns stored outbound send rows.
  - Supports optional status filtering.

## CLI

- ✅ `python -m apps.cli sync`
  - Uses the same storage and sync runner as the API.
  - Supports page limits, full pagination, and delay tuning.

- ✅ `python -m apps.cli send`
  - Uses the same send runner as the API.
  - Supports optional idempotency keys.

- ✅ CLI validation and error handling
  - Rejects invalid account IDs and invalid argument combinations.
  - Prints provider TODO text only when the provider raises `NotImplementedError` or invalid sync configuration reaches that path.

## Cookie handling and auth input

- ✅ Cookie header parsing
  - `libs/core/cookies.py` parses raw cookie header strings such as `li_at=...; JSESSIONID=...`.

- ✅ JSON cookie export parsing
  - Also accepts a JSON array format copied from browser tools.

- ✅ `li_at` validation
  - Strips whitespace.
  - Rejects empty, too-short, or space-containing values.

- ✅ Cookie-to-auth conversion
  - Builds `AccountAuth` from either supported cookie format.

## Storage and data model

- ✅ SQLite migrations
  - Baseline tables are created automatically.
  - Additional migrations add indexes, message direction checks, and the `outbound_sends` table.

- ✅ Account storage
  - Stores auth JSON and optional proxy JSON.
  - Supports auth refresh for existing accounts.

- ✅ Thread storage
  - Upserts by `(account_id, platform_thread_id)`.

- ✅ Message storage
  - Inserts inbound and outbound messages.
  - Skips duplicates via a unique `(account_id, platform_message_id)` constraint.
  - Normalizes timestamps to UTC.

- ✅ Sync cursor storage
  - Stores one cursor per `(account_id, thread_id)`.

- ✅ Outbound send tracking
  - Tracks pending, sent, and failed send attempts.
  - Supports listing and idempotent reuse by `(account_id, idempotency_key)`.

## Security and redaction

- ✅ Secret redaction in object representations
  - `AccountAuth`, `ProxyConfig`, and `LinkedInProvider` hide secrets in `__repr__` and `__str__`.

- ✅ Structured log redaction
  - `redact_for_log()` recursively redacts known sensitive keys.

- ✅ Inline string redaction
  - `redact_string()` removes sensitive values from free-form strings.

- ✅ Automatic logging filter
  - `configure_logging()` installs `SecretRedactingFilter` on the root logger.
  - The filter also scrubs dataclass log args and exception text.

- ✅ API error sanitization
  - Several `HTTPException` detail strings are passed through `redact_string()` before being returned.

- ⚠️ Encryption at rest
  - Works when `DESEARCH_ENCRYPTION_KEY` is set.
  - Falls back to plaintext storage when the key is absent, with a one-time warning.

## LinkedIn provider behavior

- ✅ Thread listing via Voyager GraphQL
  - `LinkedInProvider.list_threads()` calls the messaging GraphQL endpoint.
  - It paginates with `newSyncToken` and extracts conversation URNs and titles.

- ✅ Message fetch via Voyager GraphQL
  - `LinkedInProvider.fetch_messages()` fetches one conversation page at a time.
  - Parses message IDs, sender info, text, timestamps, and direction.
  - Returns a `next_cursor` based on the oldest fetched message timestamp when more pages may exist.

- ✅ Message sending via Voyager messaging API
  - `LinkedInProvider.send_message()` posts conversation creation payloads to LinkedIn.
  - Enforces a minimum send interval.
  - Retries network errors and backs off on rate limiting.

- ✅ Basic auth sanity check
  - `LinkedInProvider.check_auth()` validates local cookie presence and formatting.

- ⚠️ Cloudflare fallback for GraphQL
  - If GraphQL requests appear blocked, the provider can try Playwright-based cookie harvesting.
  - This requires the optional Playwright dependency and a successful browser navigation flow.

- ⚠️ Profile discovery dependency
  - GraphQL thread listing depends on a successful `/voyager/api/me` request to derive the mailbox URN.
  - Redirected or rejected `/voyager/api/me` bootstrap responses now fail explicitly with refresh guidance instead of silently caching a null profile id.
  - Blocked HTML or other unusable `/voyager/api/me` payloads now fail explicitly and leave stored auth untouched.

## Reliability and rate limiting

- ✅ Network retry handling
  - GET requests retry network failures and retryable 5xx responses.
  - Send requests retry network failures separately.

- ✅ Rate-limit backoff tracking
  - The provider tracks when rate limiting was encountered.
  - Sync responses surface this flag.

- ⚠️ LinkedIn query hash stability
  - GraphQL query IDs are hardcoded.
  - They may need manual updates when LinkedIn changes its frontend bundle.

## Feature gaps and non-goals

- ❌ Real remote auth verification in `/auth/check`
  - Current implementation does not verify cookies against LinkedIn.

- ❌ Account deletion endpoint
  - No API or CLI path removes stored accounts.

- ❌ Background worker or scheduler
  - Sync runs inline inside the request or CLI process.

- ❌ Multi-provider support
  - Only LinkedIn is implemented.

- ❌ Attachment send and attachment sync
  - Message parsing preserves raw payloads, but there is no dedicated attachment model or upload flow.

- 🚧 Playwright-assisted GraphQL resilience
  - There is an implementation path for browser cookie harvesting, but it remains operationally fragile because it depends on LinkedIn and Cloudflare behavior.
