from __future__ import annotations

import json
from pathlib import Path
import httpx

ART = Path('validation/artifacts')
BASE = 'http://127.0.0.1:8899'
checks = [
    ('health', 'GET', '/health', None, 200),
    ('ops-status', 'GET', '/ops/status', None, 200),
    ('account-health', 'GET', '/ops/accounts/1/health', None, 200),
    ('inbox', 'GET', '/ops/inbox?account_id=1&limit=5', None, 200),
    ('search', 'GET', '/ops/search?account_id=1&q=Bittensor&direction=in&limit=5', None, 200),
    ('thread-detail', 'GET', '/ops/threads/1?account_id=1', None, 200),
    ('thread-messages', 'GET', '/ops/threads/1/messages?account_id=1&limit=5', None, 200),
    ('sync-dry-run', 'POST', '/ops/sync/dry-run', {'account_id': 1, 'limit_per_thread': 25, 'max_pages_per_thread': 1, 'delay_between_threads_s': 0, 'delay_between_pages_s': 0}, 200),
    ('campaign-status', 'GET', '/ops/campaigns/73/status?account_id=1', None, 200),
    ('campaign-dry-run', 'POST', '/ops/campaigns/73/run-dry-run', {'account_id': 1, 'limit': 10}, 200),
    ('drafts', 'GET', '/ops/drafts?account_id=1&limit=10', None, 200),
    ('approvals', 'GET', '/ops/approvals?account_id=1&limit=10', None, 200),
    ('audit', 'GET', '/ops/audit?account_id=1&limit=10', None, 200),
    ('validation', 'GET', '/ops/validation/objective-73', None, 200),
    ('send-approved-refusal', 'POST', '/ops/send-approved', {'approval_id': 'appr_missing', 'account_id': 1, 'recipient': 'urn:li:msg_conversation:(synthetic,ada)', 'text': 'Refusal proof'}, 409),
]
summary = []
with httpx.Client(timeout=10.0) as client:
    for name, method, path, body, expected in checks:
        url = BASE + path
        (ART / f'api-{name}.cmd').write_text(f'{method} {url}' + (f' body={json.dumps(body, sort_keys=True)}' if body else '') + '\n')
        response = client.request(method, url, json=body)
        try:
            payload = response.json()
        except Exception:
            payload = {'raw': response.text}
        (ART / f'api-{name}.json').write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
        (ART / f'api-{name}.status').write_text(f'http_status={response.status_code}\nexpected={expected}\n')
        ok = response.status_code == expected
        if expected == 200:
            ok = ok and payload.get('ok', True) is not False
        summary.append({'name': name, 'status': response.status_code, 'expected': expected, 'ok': ok})
        if not ok:
            raise SystemExit(f'{name} returned {response.status_code}, expected {expected}: {payload}')
(ART / 'api-evidence-summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
print(json.dumps({'ok': True, 'checks': summary}, indent=2, sort_keys=True))
