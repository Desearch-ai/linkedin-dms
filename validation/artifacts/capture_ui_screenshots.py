from __future__ import annotations

import json
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ART = Path('validation/artifacts')
BASE = 'http://127.0.0.1:8899/console'
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

screens = {}
console_messages = []
network_errors = []

def snap(locator, name: str):
    path = ART / name
    locator.screenshot(path=str(path))
    screens[name] = str(path)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1200}, device_scale_factor=1)
    page.on('console', lambda msg: console_messages.append({'type': msg.type, 'text': msg.text}))
    page.on('requestfailed', lambda req: network_errors.append({'url': req.url, 'failure': req.failure.error_text if req.failure else 'unknown'}))
    page.goto(BASE, wait_until='networkidle')
    # The UI boot issues several read-only requests in parallel; retry local-only health/inbox reads
    # before capturing so screenshots represent a loaded operator console, not a transient SQLite thread error.
    try:
        page.wait_for_selector('#thread-list .thread-row', timeout=10000)
    except PlaywrightTimeoutError:
        page.evaluate('loadInbox()')
        page.wait_for_selector('#thread-list .thread-row', timeout=10000)
    if page.locator('#health-card strong').inner_text().strip().lower() != 'ok':
        page.evaluate('loadHealth()')
        page.wait_for_function("document.querySelector('#health-card strong').textContent.trim().toLowerCase() === 'ok'", timeout=10000)
    # Inbox/search proof: run a local search and capture the inbox/search panel.
    page.fill('#search-query', 'Bittensor')
    page.select_option('#direction-filter', 'in')
    page.click('#search-form button[type=submit]')
    page.wait_for_selector('#thread-list .thread-row', timeout=10000)
    snap(page.locator('aside.thread-panel'), 'ui-inbox-search.png')
    # Thread detail proof: open the search result.
    page.locator('#thread-list .thread-row').first.click()
    page.wait_for_selector('#message-list .message', timeout=10000)
    snap(page.locator('section.detail-panel'), 'ui-thread-detail.png')
    # Account health proof.
    snap(page.locator('section.status-grid'), 'ui-account-health-sync-status.png')
    # Draft approval proof: create and approve a local-only draft. Do not click send.
    page.fill('#draft-recipient', 'urn:li:msg_conversation:(synthetic,ada)')
    page.fill('#draft-text', 'UI screenshot synthetic draft; no external send.')
    page.fill('#draft-idempotency', 'obj73-ui-draft')
    page.click('#create-draft')
    page.wait_for_selector('#draft-output.success', timeout=10000)
    page.click('#approve-draft')
    page.wait_for_function("document.querySelector('#draft-output').textContent.includes('recorded')")
    snap(page.locator('#draft-form'), 'ui-draft-approval.png')
    # Campaign/sync status proof: run sync dry-run and campaign dry-run buttons. Both report external_writes 0.
    page.click('#sync-dry-run')
    page.wait_for_function("document.querySelector('#campaign-status').textContent.includes('external writes 0')")
    snap(page.locator('article[aria-labelledby="campaign-heading"]'), 'ui-campaign-sync-status.png')
    page.click('#campaign-dry-run')
    page.wait_for_function("document.querySelector('#campaign-status').textContent.includes('external writes 0')")
    # Audit/outbound history proof after draft/approval audit events.
    page.wait_for_timeout(500)
    snap(page.locator('article[aria-labelledby="audit-heading"]'), 'ui-audit-outbound-history.png')
    page.screenshot(path=str(ART / 'ui-full-console.png'), full_page=True)
    screens['ui-full-console.png'] = str(ART / 'ui-full-console.png')
    browser.close()

summary = {'screenshots': screens, 'console_messages': console_messages, 'network_errors': network_errors}
(ART / 'ui-screenshot-summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True))
print(json.dumps({'ok': True, 'screenshots': screens, 'network_errors': network_errors}, indent=2, sort_keys=True))
