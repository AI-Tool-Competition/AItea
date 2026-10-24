"""Knowledge HTTP endpoints; business rules stay in application services."""

import json
from typing import Literal

from fastapi import Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.contexts.documents.domain.documents import MAX_UPLOAD
from app.contexts.documents.domain.metadata import MetadataPatch
from app.shared.application import models as models
from app.shared.domain.errors import fail


def register(app, service, auth, user, admin_user):
    @app.get("/api/tea/documents")
    def documents(
        case_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        role: Literal["primary", "secondary"] | None = None,
        parent_id: str | None = None,
        q: str | None = Query(None, max_length=200),
        legal_level: Literal["primary", "secondary", "attachment"] | None = None,
        collection: str | None = Query(None, min_length=1, max_length=100),
        country: str | None = Query(None, pattern=r"^(?:[A-Z]{2}|unassigned)$"),
        source_scope: Literal["personal", "system"] | None = None,
        library_only: bool = False,
        current=Depends(user),
    ):
        return service.documents(
            current["id"],
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

    @app.post("/api/tea/documents", status_code=201)
    async def upload(
        file: UploadFile = File(...),
        case_id: str | None = Form(None),
        role: str = Form("primary"),
        parent_id: str | None = Form(None),
        tags: str = Form("[]"),
        current=Depends(user),
    ):
        try:
            tags_list = json.loads(tags)
        except json.JSONDecodeError:
            fail(422, "invalid_tags", "标签格式无效")
        content = bytearray()
        try:
            while chunk := await file.read(1024 * 1024):
                content.extend(chunk)
                if len(content) > MAX_UPLOAD:
                    fail(413, "file_too_large", "文件不能超过 20 MB")
            return await run_in_threadpool(
                service.store_document,
                current["id"],
                file.filename or "document.bin",
                bytes(content),
                file.content_type or "",
                case_id=case_id or None,
                role=role,
                parent_id=parent_id or None,
                tags=tags_list,
            )
        finally:
            await file.close()

    @app.get("/api/tea/documents/{id}")
    def document(
        id: str,
        element_offset: int = Query(0, ge=0),
        chunk_offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        include_preview: bool = True,
        current=Depends(user),
    ):
        return service.document_detail(
            current["id"],
            id,
            element_offset=element_offset,
            chunk_offset=chunk_offset,
            limit=limit,
            include_preview=include_preview,
        )

    @app.get("/api/tea/documents/{id}/images/{image_id}")
    def document_image(id: str, image_id: str, current=Depends(user)):
        return Response(
            service.document_image(current["id"], id, image_id),
            media_type="image/png",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @app.get("/api/tea/documents/{id}/download")
    def document_download(id: str, current=Depends(user)):
        row = service.library.download(current["id"], id)
        return FileResponse(
            row["storage_path"],
            filename=row["filename"],
            media_type="application/octet-stream",
            content_disposition_type="attachment",
            headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
        )

    @app.patch("/api/tea/documents/{id}/metadata")
    def document_metadata(id: str, payload: MetadataPatch, current=Depends(user)):
        return service.library.patch_metadata(current["id"], id, payload)

    @app.get("/api/tea/documents/{id}/versions")
    def document_versions(
        id: str, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(user)
    ):
        return service.archive.versions(current["id"], id, limit, offset)

    @app.get("/api/tea/documents/{id}/relations")
    def document_relations(
        id: str, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(user)
    ):
        return service.archive.relations(current["id"], id, limit, offset)

    @app.patch("/api/tea/documents/{id}/classification")
    def document_classification(id: str, payload: models.ClassificationPatch, current=Depends(user)):
        return service.archive.patch_classification(current["id"], id, payload.model_dump())

    @app.post("/api/tea/documents/{id}/relations", status_code=201)
    def add_document_relation(id: str, payload: models.LegalRelationInput, current=Depends(user)):
        return service.archive.add_relation(current["id"], id, payload.model_dump())

    @app.post("/api/tea/search", response_model=models.SearchResult, response_model_exclude_none=True)
    def search(payload: models.Search, current=Depends(user)):
        return service.search(
            current["id"],
            payload.query,
            payload.case_id,
            payload.limit,
            payload.as_of,
            payload.collection,
            payload.country,
            source_scope=payload.source_scope,
        )

    @app.get("/api/tea/library/countries", response_model=models.LibraryCountryList)
    def library_countries(current=Depends(user)):
        return service.checklists.library_countries()
