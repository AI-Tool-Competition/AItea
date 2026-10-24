"""In-memory RAGFlow HTTP fixture; use the real index writer and authorization query."""

import hashlib
from contextlib import closing

from app.contexts.documents.domain.text import _terms


class MemoryRAGFlow:
    def __init__(self):
        self.datasets, self.documents, self.chunks = {}, {}, {}

    def call(self, method, path, *, payload=None, params=None, files=None, timeout=None):
        if path == "/datasets":
            if method == "GET":
                return [row for row in self.datasets.values() if row["name"] == params["name"]]
            identifier = hashlib.sha256(payload["name"].encode()).hexdigest()[:20]
            self.datasets[identifier] = {"id": identifier, **payload}
            return self.datasets[identifier]
        if path.endswith("/documents"):
            if method == "GET":
                return {"docs": [row for row in self.documents.values() if row["name"] == params["keywords"]]}
            name, stream, *_ = files["file"]
            assert stream.read()
            identifier = hashlib.sha256((path + name).encode()).hexdigest()[:20]
            self.documents[identifier] = {"id": identifier, "name": name}
            return [self.documents[identifier]]
        if path.endswith("/chunks"):
            document = path.split("/")[-2]
            if method == "GET":
                return {
                    "chunks": [row for row in self.chunks.values() if row["document_id"] == document]
                    if params.get("page", 1) == 1
                    else []
                }
            content = payload["content"]
            identifier = hashlib.sha256((document + content).encode()).hexdigest()[:20]
            self.chunks[identifier] = {
                "id": identifier,
                "chunk_id": identifier,
                "document_id": document,
                "content_with_weight": content,
                "similarity": 1.0,
            }
            return {"chunk": {"id": identifier}}
        if path == "/retrieval":
            terms = set(_terms(payload["question"]))
            return {
                "chunks": [
                    row
                    for row in self.chunks.values()
                    if row["document_id"] in payload["document_ids"]
                    and terms.intersection(_terms(row["content_with_weight"]))
                ]
            }
        raise AssertionError((method, path))


def finish_indexes(service):
    """Finish fixture worker work before exercising a query; never replace the query itself."""
    with closing(service.db.connect()) as conn:
        documents = [dict(row) for row in conn.execute("SELECT * FROM documents WHERE role='primary'")]
    for document in documents:
        with service.db.transaction() as conn:
            service.index.sync_document(conn, document["id"])
            if not service.archive.can_index(conn, document["id"]):
                continue
            pending = service.ragflow.pending(conn, document["id"], service.embedding_fingerprint())
            if not pending:
                continue
            service.collection.queue_processing(conn, document["id"], document["owner_id"])
            conn.execute(
                "UPDATE processing_jobs SET phase='embed',available_at=0 WHERE document_id=?",
                (document["id"],),
            )
        claim = service.collection.claim_processing(
            owner_id=document["owner_id"], document_ids=[document["id"]]
        )
        if not claim:
            continue
        for _ in range(8):
            if service.ragflow.step(document, claim) == 0:
                break
        else:
            raise AssertionError("fixture indexing did not finish within its bound")
        with service.db.transaction() as conn:
            conn.execute(
                "UPDATE processing_jobs SET status='completed',lease_token=NULL,lease_until=NULL WHERE document_id=? AND lease_token=?",
                (document["id"], claim["lease_token"]),
            )
