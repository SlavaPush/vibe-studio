import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64",
);

test("изображение в ответе ассистента можно скачать", async ({ page }) => {
  const assetId = "4f5a6b7c-8d9e-4f0a-9b2c-3d4e5f6a7b8c";
  const runId = "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47";
  const convId = fixtures.conversation.id;
  const messages = {
    items: [
      {
        id: "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
        conversation_id: convId,
        role: "user",
        kind: null,
        text: "Нарисуй кота",
        run_id: runId,
        effective_model: null,
        effort: null,
        asset: null,
        created_at: "2026-10-23T18:30:00Z",
      },
      {
        id: "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b",
        conversation_id: convId,
        role: "assistant",
        kind: null,
        text: "",
        run_id: runId,
        effective_model: "gpt-image-2.5-flare",
        effort: "unsupported",
        asset: { id: assetId, mime_type: "image/png", width: 1, height: 1 },
        created_at: "2026-10-23T18:30:12Z",
      },
    ],
    active_run_id: null,
    runs: [],
  };
  await mockApi(page, {
    "GET /api/conversations/{id}/messages": { body: messages },
    "GET /api/assets/{id}/url": {
      body: { url: "/assets-mock/cat.png", expires_at: "2099-01-01T00:00:00Z" },
    },
  });
  await page.route("**/assets-mock/cat.png", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "image/png",
      headers: { "content-disposition": 'attachment; filename="cat.png"' },
      body: PNG,
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: fixtures.conversation.title, exact: true }).click();

  const article = page.getByRole("article", { name: "Ответ ассистента", exact: true });
  const image = article.getByRole("img", { name: "Сгенерированное изображение", exact: true });
  await expect(image).toBeVisible();

  const control = article.getByRole("link", { name: "Скачать изображение", exact: true });
  await expect(control).toBeVisible();

  const imageSrc = await image.evaluate((el) => (el as HTMLImageElement).src);
  const [download] = await Promise.all([page.waitForEvent("download"), control.click()]);
  expect(download.url()).toBe(imageSrc);
});
