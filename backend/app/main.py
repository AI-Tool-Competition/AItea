"""Stable ASGI entry point."""

from app.interfaces.http.factory import create_app

app = create_app()
