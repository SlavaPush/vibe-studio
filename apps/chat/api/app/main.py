import os

from fastapi import FastAPI

app = FastAPI(title="vibe-chat API")


@app.get("/api/version")
def version() -> dict[str, str]:
    return {"version": os.getenv("APP_VERSION", "dev")}
