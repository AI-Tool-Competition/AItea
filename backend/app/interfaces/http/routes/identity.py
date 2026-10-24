"""Identity HTTP endpoints; business rules stay in application services."""

import os

from fastapi import Depends, Query, Request, Response

from app.contexts.identity.application.auth import COOKIE
from app.shared.application import models as models


def register(app, service, auth, user, admin_user):
    def set_session(response, token):
        secure = (
            os.getenv(
                "TEA_COOKIE_SECURE", "true" if os.getenv("TEA_ENV") == "production" else "false"
            ).lower()
            == "true"
        )
        response.set_cookie(
            COOKIE, token, httponly=True, secure=secure, samesite="lax", max_age=7 * 86400, path="/"
        )

    def throttle(request):
        auth.throttle(request.client.host if request.client else "unknown")

    @app.post("/api/tea/auth/demo")
    def demo(request: Request, response: Response):
        throttle(request)
        current, token = auth.demo(request.cookies.get(COOKIE))
        set_session(response, token)
        return current

    @app.post("/api/tea/auth/register", status_code=201)
    def register(payload: models.Register, request: Request, response: Response):
        throttle(request)
        current, token = auth.register(payload.model_dump(), request.cookies.get(COOKIE))
        if token:
            set_session(response, token)
        return current

    @app.post("/api/tea/auth/login")
    def login(payload: models.Login, request: Request, response: Response):
        throttle(request)
        current, token = auth.login(payload.model_dump(), request.cookies.get(COOKIE))
        set_session(response, token)
        return current

    @app.post("/api/tea/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        auth.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")

    @app.get("/api/tea/auth/me")
    def me(current=Depends(user)):
        return current

    @app.get("/api/tea/admin/users")
    def admin_users(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        q: str = Query("", max_length=160),
        current=Depends(admin_user),
    ):
        return auth.list_users(limit=limit, offset=offset, query=q)

    @app.patch("/api/tea/admin/users/{user_id}")
    def admin_update_user(
        user_id: str,
        payload: models.UserAccessPatch,
        current=Depends(admin_user),
    ):
        return auth.update_user_access(user_id, payload.model_dump(exclude_unset=True))

    @app.post("/api/tea/admin/bootstrap")
    def bootstrap_admin(payload: models.AdminBootstrapInput, current=Depends(user)):
        return auth.bootstrap_admin(current["id"], payload.bootstrap_token)
