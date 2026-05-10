from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3

from libs.core.models import AccountAuth
from libs.core.storage import Storage, utcnow

DB = Path('validation/local-test.sqlite')
if DB.exists():
    DB.unlink()
for suffix in ('-wal', '-shm'):
    p = Path(str(DB) + suffix)
    if p.exists():
        p.unlink()

storage = Storage(db_path=DB)
storage.migrate()
account_id = storage.create_account(
    label='objective-73-synthetic',
    auth=AccountAuth(li_at='AQ' + 'ED_SYNTHETIC_ONLY_NOT_REAL', jsessionid='ajax:' + 'SYNTHETIC_' + 'CSRF'),
)
base = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)
thread_ada = storage.upsert_thread(
    account_id=account_id,
    platform_thread_id='urn:li:msg_conversation:(synthetic,ada)',
    title='Ada Lovelace (synthetic)',
)
storage.insert_message(
    account_id=account_id,
    thread_id=thread_ada,
    platform_message_id='synthetic-msg-ada-1',
    direction='in',
    sender='Ada Lovelace',
    text='Interested in Bittensor subnet ops; synthetic cookie ' + 'li_at=AQ' + 'ED_DO_NOT_LEAK should redact.',
    sent_at=base,
    raw={'source': 'validation-seed'},
)
storage.insert_message(
    account_id=account_id,
    thread_id=thread_ada,
    platform_message_id='synthetic-msg-ada-2',
    direction='out',
    sender='Desearch Operator',
    text='Thanks Ada — drafting a local-only follow-up for Objective 73 validation.',
    sent_at=base + timedelta(minutes=5),
    raw={'source': 'validation-seed'},
)
thread_grace = storage.upsert_thread(
    account_id=account_id,
    platform_thread_id='urn:li:msg_conversation:(synthetic,grace)',
    title='Grace Hopper (synthetic)',
)
storage.insert_message(
    account_id=account_id,
    thread_id=thread_grace,
    platform_message_id='synthetic-msg-grace-1',
    direction='in',
    sender='Grace Hopper',
    text='Can you share the campaign dry-run status and audit trail?',
    sent_at=base + timedelta(minutes=10),
    raw={'source': 'validation-seed'},
)
# Local draft + approved evidence. No provider send is executed by this seeding script.
draft = storage.create_draft_reply(
    account_id=account_id,
    thread_id=thread_ada,
    recipient='urn:li:msg_conversation:(synthetic,ada)',
    text='Synthetic approved draft; do not send externally.',
    campaign_id=73,
    idempotency_key='obj73-approved-synthetic',
)
storage.approve_send_approval(draft['approval_id'], approved_by='objective-73-local-validation')
# Add campaign/outbound rows for status/audit screens without calling LinkedIn.
now = utcnow().isoformat()
conn = sqlite3.connect(DB)
conn.execute(
    "INSERT INTO campaigns(id, account_id, name, state, rate_limit_daily_cap, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
    (73, account_id, 'Objective 73 synthetic dry-run campaign', 'active', 25, now, now),
)
conn.execute(
    "INSERT INTO campaign_recipients(campaign_id, recipient, thread_id, draft_id, approval_id, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    (73, 'urn:li:msg_conversation:(synthetic,ada)', thread_ada, draft['draft_id'], draft['approval_id'], 'approved', now, now),
)
conn.commit()
conn.close()
send_id, _ = storage.create_or_get_outbound_send(
    account_id=account_id,
    idempotency_key='obj73-failed-synthetic',
    recipient='urn:li:msg_conversation:(synthetic,grace)',
    text='Synthetic failed-send ledger row; no provider call happened.',
)
storage.mark_outbound_failed(send_id=send_id, error='synthetic validation failure row; no external send attempted')
storage.record_ops_audit_event(
    account_id=account_id,
    event_type='validation.seeded',
    actor='objective-73-local-validation',
    entity_type='validation',
    entity_id='objective-73',
    payload={'external_sends': 0, 'data_source': 'synthetic local SQLite seed'},
)
print({'db': str(DB), 'account_id': account_id, 'thread_ids': [thread_ada, thread_grace], 'approval_id': draft['approval_id'], 'draft_id': draft['draft_id'], 'campaign_id': 73})
storage.close()
