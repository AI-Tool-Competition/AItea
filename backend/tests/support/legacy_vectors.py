"""Seed/read historical vector rows without invoking the current RAGFlow queue."""


def checkpoint_inputs(conn, document_id, fingerprint, limit=128):
    return [
        dict(row)
        for row in conn.execute(
            """SELECT c.* FROM retrieval_chunks c LEFT JOIN retrieval_vectors v
        ON v.chunk_rowid=c.rowid AND v.fingerprint=? AND v.input_hash=c.input_hash
        WHERE c.document_id=? AND v.chunk_rowid IS NULL ORDER BY c.ordinal,c.rowid LIMIT ?""",
            (fingerprint, document_id, limit),
        )
    ]


def schema19_snapshot(service):
    """Create a pre-MinerU/RAGFlow fixture for the explicit legacy export tool."""
    with service.db.transaction() as conn:
        for table in (
            "agent_tool_receipts",
            "agent_runs",
            "agent_memory_entries",
            "agent_session_summaries",
            "ragflow_chunks",
            "ragflow_documents",
            "ragflow_datasets",
            "document_parse_tasks",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM schema_migrations WHERE version>19")
