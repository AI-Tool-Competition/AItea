import hashlib
import logging
import os
import threading
from pathlib import Path

from app.contexts.documents.infrastructure.catalog import LegalCatalog
from app.shared.domain.values import dump
from app.shared.infrastructure.persistence.database import Database

logger = logging.getLogger(__name__)


class Service:
    def __init__(self, data_dir, *, require_existing=False):
        from app.contexts.ingestion.application.collection_jobs import CollectionService
        from app.contexts.documents.application.archive import LegalArchive
        from app.contexts.workflows.application.workflow_studio import AdminStudio

        self.db = Database(Path(data_dir), require_existing=require_existing)
        self.catalog = LegalCatalog(self.db)
        self.studio = AdminStudio(self)
        self._chat_lock = threading.Lock()
        self._active_chats = set()
        self.archive = LegalArchive(self)
        self.collection = CollectionService(self)
        from app.contexts.ingestion.application.checklists import ChecklistService

        self.checklists = ChecklistService(self)
        from app.contexts.legal_analysis.application.analysis_service import LegalService

        self.legal = LegalService(self)
        from app.contexts.documents.application.retrieval import RetrievalIndex

        self.index = RetrievalIndex(self)
        from app.contexts.documents.infrastructure.providers.ragflow import RAGFlowIndex

        self.ragflow = RAGFlowIndex(self)
        from app.contexts.conversations.application.node_workspace import NodeWorkspace

        self.node_workspace = NodeWorkspace(self)
        from app.contexts.workflows.application.configurations import TemplateService

        self.configurations = TemplateService(self)
        from app.contexts.workflows.application.workspaces import WorkspaceService

        self.workspaces = WorkspaceService(self)
        from app.contexts.documents.application.library import DocumentService

        self.library = DocumentService(self)
        from app.contexts.conversations.application.conversations import ConversationService

        self.conversations = ConversationService(self)
        from app.contexts.conversations.application.memory import AgentMemory

        self.agent_memory = AgentMemory(self)
        from app.contexts.drafting.application.drafting import ArtifactService

        self.artifacts = ArtifactService(self)
        from app.contexts.ingestion.application.source_management import SourceService

        self.source_management = SourceService(self)

    @property
    def capabilities(self):
        live = bool(os.getenv("AGENT_API_KEY") or os.getenv("LLM_API_KEY"))
        return {
            "assistant_mode": "live" if live else "demo",
            "retrieval_mode": "ragflow" if self.embedding_fingerprint() else "unavailable",
            "parser": "mineru",
        }

    def embedding_fingerprint(self):
        from app.contexts.documents.infrastructure.providers.ragflow import profile

        return profile()

    def rerank_fingerprint(self):
        model = os.getenv("RAGFLOW_RERANK_MODEL", "").strip()
        identity = [self.embedding_fingerprint(), model]
        if os.getenv("RERANK_BASE_URL") or os.getenv("RERANK_MODEL"):
            identity.extend([os.getenv("RERANK_BASE_URL"), os.getenv("RERANK_MODEL")])
        return hashlib.sha256(dump(identity).encode()).hexdigest() if model or any(identity[2:]) else None

    def template(self, owner, template_id, conn=None):
        return self.configurations.template(owner, template_id, conn)

    def templates(self, owner):
        return self.configurations.templates(owner)

    def validate_template(self, payload):
        return self.configurations.validate_template(payload)

    def publish(self, owner, payload, expected_draft_revision=None):
        return self.configurations.publish(owner, payload, expected_draft_revision)

    def own_case(self, conn, owner, case_id):
        return self.workspaces.own_case(conn, owner, case_id)

    def event(self, conn, case_id, type, title, detail):
        return self.workspaces.event(conn, case_id, type, title, detail)

    def touch(self, conn, case_id):
        return self.workspaces.touch(conn, case_id)

    def case_detail(self, owner, case_id, conn=None):
        return self.workspaces.case_detail(owner, case_id, conn)

    def cases(self, owner, limit=100, offset=0):
        return self.workspaces.cases(owner, limit, offset)

    def create_case(self, owner, values):
        return self.workspaces.create_case(owner, values)

    def get_task(self, case, task_id, *, require_unlocked=True):
        return self.workspaces.get_task(case, task_id, require_unlocked=require_unlocked)

    def check_revision(self, task, expected):
        return self.workspaces.check_revision(task, expected)

    def invalidate_task(self, conn, case_id, task_id, *, reason):
        return self.workspaces.invalidate_task(conn, case_id, task_id, reason=reason)

    def normalize_values(self, task, supplied):
        return self.workspaces.normalize_values(task, supplied)

    def patch_task(self, owner, case_id, task_id, payload):
        return self.workspaces.patch_task(owner, case_id, task_id, payload)

    def checks(self, conn, task, confirm=False):
        return self.workspaces.checks(conn, task, confirm)

    def verify_task(self, owner, case_id, task_id, payload):
        return self.workspaces.verify_task(owner, case_id, task_id, payload)

    def own_document(self, conn, owner, id):
        return self.library.own_document(conn, owner, id)

    def read_document(self, conn, owner, id):
        return self.library.read_document(conn, owner, id)

    def document_dict(self, row, conn=None):
        return self.library.document_dict(row, conn)

    def document_base(self, row):
        return self.library.document_base(row)

    def document_summaries(self, rows, conn):
        return self.library.document_summaries(rows, conn)

    def metadata_changed(self, conn, document):
        return self.library.metadata_changed(conn, document)

    def store_document(
        self,
        owner,
        filename,
        content,
        mime_type="",
        *,
        case_id=None,
        role="primary",
        parent_id=None,
        tags=None,
        source_url=None,
    ):
        return self.library.store_document(
            owner,
            filename,
            content,
            mime_type,
            case_id=case_id,
            role=role,
            parent_id=parent_id,
            tags=tags,
            source_url=source_url,
        )

    def documents(
        self,
        owner,
        case_id=None,
        limit=100,
        offset=0,
        *,
        role=None,
        parent_id=None,
        q=None,
        legal_level=None,
        collection=None,
        country=None,
        source_scope=None,
        library_only=False,
    ):
        return self.library.documents(
            owner,
            case_id,
            limit,
            offset,
            role=role,
            parent_id=parent_id,
            q=q,
            legal_level=legal_level,
            collection=collection,
            country=country,
            source_scope=source_scope,
            library_only=library_only,
        )

    def document_detail(
        self, owner, document_id, *, element_offset=0, chunk_offset=0, limit=100, include_preview=True
    ):
        return self.library.document_detail(
            owner,
            document_id,
            element_offset=element_offset,
            chunk_offset=chunk_offset,
            limit=limit,
            include_preview=include_preview,
        )

    def document_image(self, owner, document_id, image_id):
        return self.library.document_image(owner, document_id, image_id)

    def search(
        self,
        owner,
        query,
        case_id=None,
        limit=5,
        as_of=None,
        collection=None,
        country=None,
        document_ids=None,
        *,
        source_scope=None,
    ):
        return self.library.search(
            owner, query, case_id, limit, as_of, collection, country, document_ids, source_scope=source_scope
        )

    def search_eligible(
        self,
        owner,
        query,
        case_id,
        limit,
        as_of,
        collection=None,
        country=None,
        document_ids=None,
        *,
        historical=False,
        source_scope=None,
    ):
        return self.library.search_eligible(
            owner,
            query,
            case_id,
            limit,
            as_of,
            collection,
            country,
            document_ids,
            historical=historical,
            source_scope=source_scope,
        )

    def insert_message(self, conn, case_id, stage_id, task_id, role, content, citations, trace):
        return self.conversations.insert_message(
            conn, case_id, stage_id, task_id, role, content, citations, trace
        )

    def messages(self, owner, case_id, stage_id=None, limit=100, offset=0, conversation_id=None):
        return self.conversations.messages(owner, case_id, stage_id, limit, offset, conversation_id)

    def run_workflow(self, owner, template, stage, content, tasks, **options):
        return self.conversations.run_workflow(owner, template, stage, content, tasks, **options)

    def prepare_message(self, owner, case_id, payload):
        return self.conversations.prepare_message(owner, case_id, payload)

    def post_message(self, owner, case_id, payload, *, emit=None, check_cancelled=None):
        return self.conversations.post_message(
            owner, case_id, payload, emit=emit, check_cancelled=check_cancelled
        )

    def finish_message(self, owner, case_id, payload, prepared, emit, check_cancelled):
        return self.conversations.finish_message(owner, case_id, payload, prepared, emit, check_cancelled)

    def artifact_dict(self, row):
        return self.artifacts.artifact_dict(row)

    def own_artifact(self, conn, owner, id):
        return self.artifacts.own_artifact(conn, owner, id)

    def create_artifact(self, owner, case_id, payload, *, content=None, conn=None):
        return self.artifacts.create_artifact(owner, case_id, payload, content=content, conn=conn)

    def patch_artifact(self, owner, id, payload):
        return self.artifacts.patch_artifact(owner, id, payload)

    def confirm_artifact(self, owner, id, version):
        return self.artifacts.confirm_artifact(owner, id, version)

    def source_dict(self, row, conn=None, latest_run=...):
        return self.source_management.source_dict(row, conn, latest_run)

    def sources(self, owner, limit=100, offset=0):
        return self.source_management.sources(owner, limit, offset)

    def save_source(self, owner, payload, id=None, conn=None):
        return self.source_management.save_source(owner, payload, id, conn)

    def due_sources(self, *, worker_scope=None):
        return self.source_management.due_sources(worker_scope=worker_scope)

    def fetch_source(self, owner_id, source_id, payload=None):
        return self.source_management.fetch_source(owner_id, source_id, payload)
