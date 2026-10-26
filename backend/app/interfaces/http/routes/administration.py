"""Administrator-only instance settings; never return provider credentials."""

from fastapi import Depends

from app.shared.application import models


def register(app, service, auth, user, admin_user):
    @app.get("/api/tea/admin/api-settings", response_model=models.ApiSettings)
    def api_settings(current=Depends(admin_user)):
        return app.state.api_settings.read()

    @app.patch("/api/tea/admin/api-settings", response_model=models.ApiSettings)
    def save_api_settings(payload: models.ApiSettingsPatch, current=Depends(admin_user)):
        return app.state.api_settings.update(payload)
