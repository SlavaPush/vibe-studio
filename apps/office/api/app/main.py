from fastapi import FastAPI

app = FastAPI(title="back-office API")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
