import os

from fastapi import FastAPI

from app.models_catalog import MODEL_CATALOG
from app.schemas import ModelCatalog

app = FastAPI(title="vibe-chat API")


@app.get("/api/version")
def version() -> dict[str, str]:
    return {"version": os.getenv("APP_VERSION", "dev")}


@app.get("/api/models")
def models() -> ModelCatalog:
    return MODEL_CATALOG
