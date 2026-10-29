import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

const convId = fixtures.conversation.id;

function msg(id: string, role: "user" | "assistant", text: string, runId: string) {
  return {
    id,
    conversation_id: convId,
    role,
    kind: null,
    text,
    run_id: runId,
    effective_model: role === "assistant" ? fixtures.model : null,
    effort: role === "assistant" ? "high" : null,
    asset: null,
    created_at: "2026-10-23T18:30:00Z",
  };
}

function run(id: string, userId: string, assistantId: string) {
  return {
    id,
    conversation_id: convId,
    status: "completed",
    requested_model: "claude-sonnet-5-5",
    effective_model: fixtures.model,
    effort: "high",
    intent: "chat",
    complexity: "standard",
    risk: "none",
    switch_reason: null,
    user_message_id: userId,
    assistant_message_id: assistantId,
    error: null,
    created_at: "2026-10-23T18:30:00Z",
    updated_at: "2026-10-23T18:30:12Z",
  };
}

test("кнопка Скопировать у ответов ассистента", async ({ page }) => {
  const run1 = "0b8d6f24-7a3e-4c91-b2d5-000000000001";
  const run2 = "0b8d6f24-7a3e-4c91-b2d5-000000000002";
  const u1 = "1c2d3e4f-5a6b-4c7d-8e9f-000000000001";
  const a1 = "3e4f5a6b-7c8d-4e9f-8a1b-000000000001";
  const u2 = "1c2d3e4f-5a6b-4c7d-8e9f-000000000002";
  const a2 = "3e4f5a6b-7c8d-4e9f-8a1b-000000000002";
  const answer1 = "Первый ответ ассистента";
  const answer2 = "Второй ответ ассистента";

  await page.addInitScript(() => {
    (window as unknown as { __copied: string[] }).__copied = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (t: string) => {
          (window as unknown as { __copied: string[] }).__copied.push(t);
        },
        readText: async () => "",
      },
    });
  });

  await mockApi(page, {
    "GET /api/conversations/{id}/messages": {
      body: {
        items: [
          msg(u1, "user", "Первый вопрос", run1),
          msg(a1, "assistant", answer1, run1),
          msg(u2, "user", "Второй вопрос", run2),
          msg(a2, "assistant", answer2, run2),
        ],
        active_run_id: null,
        runs: [run(run1, u1, a1), run(run2, u2, a2)],
      },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: fixtures.conversation.title, exact: true }).click();

  await expect(page.getByText(answer2, { exact: true })).toBeVisible();
  await expect(page.getByText("Второй вопрос", { exact: true })).toBeVisible();

  const buttons = page.getByRole("button", { name: "Скопировать", exact: true });
  await expect(buttons).toHaveCount(2);

  const articles = page.getByRole("article", { name: "Ответ ассистента" });
  await expect(articles).toHaveCount(2);
  await expect(articles.nth(0).getByRole("button", { name: "Скопировать", exact: true })).toBeVisible();
  await expect(articles.nth(1).getByRole("button", { name: "Скопировать", exact: true })).toBeVisible();

  await articles.nth(1).getByRole("button", { name: "Скопировать", exact: true }).click();

  await expect(articles.nth(1).getByRole("button", { name: "Скопировано", exact: true })).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __copied: string[] }).__copied))
    .toEqual([answer2]);
  await expect(articles.nth(0).getByRole("button", { name: "Скопировать", exact: true })).toBeVisible();

  await page.waitForTimeout(1000);
  await expect(articles.nth(1).getByRole("button", { name: "Скопировано", exact: true })).toBeVisible();

  await expect(articles.nth(1).getByRole("button", { name: "Скопировать", exact: true })).toBeVisible({ timeout: 4000 });
  await expect(page.getByRole("button", { name: "Скопировано", exact: true })).toHaveCount(0);
});
