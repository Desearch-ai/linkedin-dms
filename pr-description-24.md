# feat: implement list_threads + fetch_messages via GraphQL (#4, #5)

## Problem

The REST Voyager messaging endpoints referenced in issues #4 and #5 have been deprecated by LinkedIn:
```
GET /voyager/api/messaging/conversations              → HTTP 400
GET /voyager/api/messaging/conversations/{id}/events  → HTTP 400
```

LinkedIn migrated messaging to internal GraphQL:

| Deprecated (HTTP 400) | Working replacement |
|---|---|
| `/voyager/api/messaging/conversations` | `/voyagerMessagingGraphQL/graphql?queryId=messengerConversations.{hash}` |
| `/voyager/api/messaging/conversations/{id}/events` | `/voyagerMessagingGraphQL/graphql?queryId=messengerMessages.{hash}` |

On some networks (datacenter IPs), the GraphQL endpoints additionally enforce **Cloudflare bot-management cookies** (`__cf_bm`, `bcookie`, `bscookie`, `lidc`) that require a real browser to generate.

## Solution

Both methods first try with **basic cookies only** (`li_at` + `JSESSIONID` via httpx). If Cloudflare blocks the request (302/403 HTML), they **automatically fall back** to harvesting full browser cookies via Playwright — but **only if Playwright is installed**. If it isn't, a clear error is raised with install instructions.

```
                  basic cookies
list_threads() ───────────────────→ GraphQL API ✓  (residential IPs)
                                        │
                                   CF blocks?
                                        │
                  Playwright (optional)  ▼
                  ───────────────→ harvest CF cookies → retry GraphQL ✓
```

**Playwright is an optional dependency** — `pip install desearch-dms[browser]`. On residential IPs / VPNs where Cloudflare doesn't challenge, it's not needed at all.

### What's implemented

| Method | Behavior |
|---|---|
| `list_threads()` | `messengerConversations` GraphQL with syncToken pagination, dedup, rate limiting |
| `fetch_messages()` | `messengerMessages` GraphQL with `createdBefore` cursor, direction detection, dedup |
| `_get_profile_id()` | `/voyager/api/me` → cached profile URN for direction detection |
| `_harvest_cookies_playwright()` | Optional: headless Chromium → full cookie jar incl. Cloudflare tokens |
| `_get_with_retry()` | Exponential backoff (2s→4s→8s), honours `Retry-After` on 429 |
| `invalidate_cookies()` | Clear cached cookies to force re-harvest on Cloudflare expiry |

### What's preserved (no changes)

- `send_message()` — untouched, uses upstream REST endpoint + retry logic
- `check_auth()` — untouched
- All upstream constants, helpers (`_build_headers`, `_get_cookies`, `_proxy_url`, etc.)

## Changes

- **`libs/providers/linkedin/provider.py`** — Implement `list_threads` and `fetch_messages` with GraphQL + optional Playwright fallback
- **`pyproject.toml`** — Add `[browser]` optional extra for Playwright
- **`tests/test_list_threads.py`** — 57 tests for list_threads (new)
- **`tests/test_fetch_messages.py`** — 44 tests for fetch_messages (new)
- **`tests/test_sync_send.py`** — Update 1 test (was testing NotImplementedError stub; now tests missing-JSESSIONID error)

## Testing — 263 tests, 0 failures

```
$ python -m pytest tests/ -v
263 passed in 0.85s
```

All Playwright usage is **mocked** — no real browser needed for tests. All upstream tests pass unmodified (except the 501-stub test which now correctly tests the implemented behavior).

## Edge cases

| Scenario | Behavior |
|---|---|
| Empty inbox | `[]` |
| Cloudflare blocks | Auto-fallback to Playwright (if installed) |
| Playwright not installed + no CF block | Works fine with basic cookies |
| Playwright not installed + CF blocks | Clear `RuntimeError` with install instructions |
| Fewer than `limit` messages | `next_cursor = None` |
| Duplicate messages / threads | Deduplicated by ID/URN |
| Non-JSON / HTML error response | Treated as empty, no crash |
| Missing JSESSIONID | `ValueError` before any request |
| Profile ID unavailable | `RuntimeError` with actionable message |
| syncToken unchanged | Pagination stops |
| Max pages cap (50) | Prevents infinite loops |
| Proxy configured | Forwarded to both Playwright and httpx |
| `Retry-After` on 429 | Honoured |

## Setup

```bash
pip install -e ".[test]"
python -m pytest tests/ -v    # 263 passed

# Optional — only needed if Cloudflare blocks basic cookies:
pip install -e ".[browser]"
playwright install chromium
```

Closes #4
Closes #5
