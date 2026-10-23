import httpx

from app.config import Settings

UPLOAD_ATTEMPTS = 3
SIGNED_URL_SECONDS = 600


class StorageError(Exception):
    pass


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.base = f"{settings.supabase_url.rstrip('/')}/storage/v1"
        self.bucket = settings.storage_bucket
        self.headers = {"apikey": settings.supabase_secret_key, "Authorization": f"Bearer {settings.supabase_secret_key}"}
        self.client = httpx.AsyncClient(timeout=30)

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        for _ in range(UPLOAD_ATTEMPTS):
            try:
                response = await self.client.post(
                    f"{self.base}/object/{self.bucket}/{path}",
                    headers={**self.headers, "Content-Type": content_type, "x-upsert": "true"},
                    content=data,
                )
            except httpx.HTTPError:
                continue
            if response.status_code == 200:
                return
        raise StorageError("upload failed")

    async def signed_url(self, path: str) -> str:
        try:
            response = await self.client.post(
                f"{self.base}/object/sign/{self.bucket}/{path}",
                headers=self.headers,
                json={"expiresIn": SIGNED_URL_SECONDS},
            )
            signed = response.json().get("signedURL") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as error:
            raise StorageError("sign failed") from error
        if not signed:
            raise StorageError("sign failed")
        return f"{self.base}{signed}"

    async def close(self) -> None:
        await self.client.aclose()
