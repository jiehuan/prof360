"""ASGI entry point: `uvicorn p360_bff.main:app`."""

from p360_bff.app import create_app

app = create_app()
