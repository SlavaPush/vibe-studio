import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

const PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const IMAGE_MODEL = "gpt-image-2.5-flare";
const NOW = "2026-10-23T18:30:00Z";

type Stage = "hold" | "done" | "fail";

function buildEvents(runId: string, conversationId: string, stage: Stage): string {
  const suffix = runId.slice(-12);
  const assistantId = `3e4f5a6b-7c8d-4e9f-8a1b-${suffix}`;
  const asset = { id: `4f5a6b7c-8d9e-4f0a-9b2c-${suffix}`, mime_type: "image/png", width: 1024, height: 1024 };
  const assistant = {
    id: assistantId,
    conversation_id: conversationId,
    role: "assistant",
    kind: null,
    text: "",
    run_id: runId,
    effective_model: IMAGE_MODEL,
    effort: "unsupported",
    asset,
    created_at: NOW,
  };
  const items: [string, Record<string, unknown>][] = [
    ["accepted", { user_message_id: `1c2d3e4f-5a6b-4c7d-8e9f-${suffix}`, requested_model: "auto" }],
    ["checks_passed", { intent: "image", complexity: "standard", risk: "none" }],
    [
      "model_selected",
      { assistant_message_id: assistantId, provider: "openai", effective_model: IMAGE_MODEL, effort: "unsupported" },
    ],
  ];
  if (stage === "done") {
    items.push(["asset_ready", { message_id: assistantId, asset }]);
    items.push(["completed", { message: assistant }]);
  } else if (stage === "fail") {
    items.push([
      "failed",
      {
        error: {
          code: "provider_unavailable",
          message: "Модели временно недоступны. Повторите запрос позже.",
          retryable: true,
          operation_id: "op_office_mock",
        },
      },
    ]);
  }
  return items
    .map(([type, payload], index) => {
      const event = {
        event_id: `7d2e9f10-${String(index + 1).padStart(4, "0")}-4b3c-9d4e-${suffix}`,
        run_id: runId,
        seq: index + 1,
        type,
        timestamp: NOW,
        payload,
      };
      return `id: ${index + 1}\nevent: ${type}\ndata: ${JSON.stringify(event)}\n\n`;
    })
    .join("");
}

test("лоадер генерации изображения: виден до результата, исчезает при картинке и при ошибке", async ({ page }) => {
  let stage: Stage = "hold";
  await mockApi(page, {
    "GET /api/runs/{id}/events": ({ params }) => ({
      contentType: "text/event-stream",
      headers: { "cache-control": "no-store" },
      body: buildEvents(params.id, fixtures.conversation.id, stage),
    }),
    "GET /api/assets/{id}/url": () => ({
      body: { url: PNG, expires_at: new Date(Date.now() + 3600 * 1000).toISOString() },
    }),
  });

  // Количество анимированных элементов в ответах ассистента, кроме индикатора «Думаю».
  const animatedCount = () =>
    page.evaluate(() => {
      let n = 0;
      const articles = document.querySelectorAll('article[aria-label="Ответ ассистента"]');
      for (const article of Array.from(articles)) {
        for (const el of Array.from(article.querySelectorAll("*"))) {
          if (getComputedStyle(el).animationName === "none") continue;
          const status = el.closest('[role="status"]');
          if (status && (status.textContent || "").includes("Думаю")) continue;
          n += 1;
        }
      }
      return n;
    });

  await page.goto("/");
  const input = page.getByLabel("Сообщение", { exact: true });
  await expect(input).toBeVisible();

  // Критерий 1: пока изображение не получено, в ленте есть анимированный лоадер.
  await input.fill("Нарисуй кота в скафандре");
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  await expect(page.getByText("Нарисуй кота в скафандре", { exact: true })).toBeVisible();
  await expect.poll(animatedCount, { timeout: 10000 }).toBeGreaterThan(0);

  // Критерий 2: изображение получено — лоадер исчезает, появляется картинка.
  stage = "done";
  const image = page.getByRole("img", { name: "Сгенерированное изображение", exact: true });
  await expect(image).toBeVisible({ timeout: 20000 });
  await expect.poll(animatedCount, { timeout: 10000 }).toBe(0);

  // Критерий 3: генерация завершилась ошибкой — лоадер исчезает.
  stage = "hold";
  await input.fill("Нарисуй собаку");
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  await expect(page.getByText("Нарисуй собаку", { exact: true })).toBeVisible();
  await expect.poll(animatedCount, { timeout: 10000 }).toBeGreaterThan(0);
  stage = "fail";
  await expect.poll(animatedCount, { timeout: 20000 }).toBe(0);
});
