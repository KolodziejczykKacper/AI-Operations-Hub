"""Local REST API (FastAPI). Routes are thin: all business logic lives in the services."""

from ai_ops_hub.api.app import create_app

__all__ = ["create_app"]
