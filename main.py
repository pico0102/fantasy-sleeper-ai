"""Root FastAPI entrypoint re-exporting app from app.main."""

from app.main import app

__all__ = ["app"]
