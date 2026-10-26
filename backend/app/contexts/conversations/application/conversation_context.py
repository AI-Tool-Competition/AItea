"""Bounded provider history, committed with the visible turn and scoped to its owner."""

import hashlib
import json
from contextlib import closing

from app.contexts.conversations.application.memory import context_hash
from app.contexts.conversations.infrastructure.agent_transport import provider_settings
from app.shared.domain.values import dump, now


def context_identity(service, owner, case, stage_id, conversation_id, system, tools):
    with closing(service.db.connect()) as conn:
        service.agent_memory.scope(conn, owner, case["id"], stage_id, conversation_id)
        notes = conn.execute(
            """SELECT COUNT(*),COALESCE(SUM(revision),0) FROM agent_memory_entries
            WHERE owner_id=? AND case_id=? AND stage_id=?
            AND (conversation_id IS NULL OR conversation_id=?)""",
            (owner, case["id"], stage_id, conversation_id),
        ).fetchall()
    settings = provider_settings()
    signature = hashlib.sha256(
        dump(
            {
                "version": 1,
                "system": system,
                "tools": tools,
                "business": context_hash(case),
                "notes": [tuple(row) for row in notes],
                "provider": settings,
            }
        ).encode()
    ).hexdigest()
    # Stable routing survives restarts. No account, business ID or credential is
    # sent in cleartext as the gateway's session identifier.
    key = (
        "aitea-agent-"
        + hashlib.sha256(dump([owner, case["id"], stage_id, conversation_id]).encode()).hexdigest()[:40]
    )
    return signature, key


def read_context(service, owner, case_id, stage_id, conversation_id, signature, *, budget=120000):
    with closing(service.db.connect()) as conn:
        service.agent_memory.scope(conn, owner, case_id, stage_id, conversation_id)
        row = conn.execute(
            """SELECT c.messages FROM agent_conversation_context c
            WHERE c.conversation_id=? AND c.owner_id=? AND c.case_id=? AND c.stage_id=?
            AND c.signature=? AND c.source_message_id=(
                SELECT m.id FROM messages m JOIN conversation_messages cm ON cm.message_id=m.id
                WHERE cm.conversation_id=? AND m.case_id=? AND m.stage_id=?
                ORDER BY m.created_at DESC,m.id DESC LIMIT 1)""",
            (conversation_id, owner, case_id, stage_id, signature, conversation_id, case_id, stage_id),
        ).fetchone()
    # Leave room for the current question/context and one bounded tool round.
    return json.loads(row[0]) if row and len(row[0].encode()) <= min(120000, budget) else None


def save_context(conn, owner, case_id, stage_id, conversation_id, answer_id, value):
    if not value or not conversation_id:
        return
    serialized = dump(value["messages"])
    if len(serialized.encode()) > 200000:
        return
    conn.execute(
        """INSERT INTO agent_conversation_context
        (conversation_id,owner_id,case_id,stage_id,signature,source_message_id,messages,updated_at)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
        signature=excluded.signature,source_message_id=excluded.source_message_id,
        messages=excluded.messages,updated_at=excluded.updated_at""",
        (conversation_id, owner, case_id, stage_id, value["signature"], answer_id, serialized, now()),
    )
