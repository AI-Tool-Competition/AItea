"""Drafting HTTP endpoints; business rules stay in application services."""

from urllib.parse import quote

from fastapi import Depends, Response

from app.shared.application import models as models


def register(app, service, auth, user, admin_user):
    @app.post("/api/tea/cases/{case_id}/artifacts", status_code=201)
    def create_artifact(case_id: str, payload: models.NewArtifact, current=Depends(user)):
        return service.create_artifact(current["id"], case_id, payload.model_dump())

    @app.patch("/api/tea/artifacts/{id}")
    def patch_artifact(id: str, payload: models.ArtifactPatch, current=Depends(user)):
        return service.patch_artifact(current["id"], id, payload.model_dump())

    @app.post("/api/tea/artifacts/{id}/confirm")
    def confirm_artifact(id: str, payload: models.ArtifactConfirm, current=Depends(user)):
        return service.confirm_artifact(current["id"], id, payload.expected_version)

    @app.get("/api/tea/artifacts/{id}/versions")
    def versions(id: str, current=Depends(user)):
        return service.artifacts.versions(current["id"], id)

    @app.get("/api/tea/artifacts/{id}/download")
    def artifact_download(id: str, current=Depends(user)):
        row = service.artifacts.download(current["id"], id)
        return Response(
            row["content"],
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["title"] + ".md")},
        )
