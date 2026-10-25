import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { mockApi, fixtures } from "./support/mock-api";

test("экспорт активного диалога в Markdown", async ({ page }) => {
  const convId = fixtures.conversation.id;
  const userText = "Привет, расскажи про скафандры";
  const assistantText = "Скафандр защищает от вакуума";
  let filled = false;

  const msg = (id: string, role: string, text: string, time: string) => ({
    id,
    conversation_id: convId,
    role,
    kind: null,
    text,
    run_id: null,
    effective_model: role === "assistant" ? fixtures.model : null,
    effort: role === "assistant" ? "high" : null,
    asset: null,
    created_at: time,
  });

  await mockApi(page, {
    "GET /api/conversations/{id}/messages": () => ({
      body: filled
        ? {
            items: [
              msg("1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f", "user", userText, "2026-10-23T18:30:00Z"),
              msg("3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b", "assistant", assistantText, "2026-10-23T18:30:10Z"),
            ],
            active_run_id: null,
            runs: [],
          }
        : { items: [], active_run_id: null, runs: [] },
    }),
  });

  const exportButton = () => page.getByRole("button", { name: "Экспорт в Markdown", exact: true });
  const openConversation = async () => {
    await page.getByRole("button", { name: fixtures.conversation.title, exact: true }).click();
  };

  // Пустой диалог: кнопка недоступна или отсутствует
  await page.goto("/");
  await openConversation();
  await expect(page.getByLabel("Сообщение", { exact: true })).toBeVisible();
  await expect(exportButton()).toBeDisabled();

  // Диалог с сообщениями
  filled = true;
  await page.reload();
  await openConversation();
  await expect(page.getByText(userText, { exact: true })).toBeVisible();
  await expect(page.getByText(assistantText, { exact: true })).toBeVisible();
  await expect(exportButton()).toBeVisible();
  await expect(exportButton()).toBeEnabled();

  // Клавиатура: фокус и Enter
  await exportButton().focus();
  await expect(exportButton()).toBeFocused();
  const [download] = await Promise.all([page.waitForEvent("download"), page.keyboard.press("Enter")]);

  expect(download.suggestedFilename().endsWith(".md")).toBe(true);
  const path = await download.path();
  const content = await readFile(path, "utf-8");
  const lines = content.split(/\r?\n/);
  expect(lines[0]).toBe("# " + fixtures.conversation.title);

  const iUserRole = content.indexOf("Пользователь");
  const iUser = content.indexOf(userText);
  const iAssistantRole = content.indexOf("Ассистент");
  const iAssistant = content.indexOf(assistantText);
  expect(iUserRole).toBeGreaterThan(-1);
  expect(iUser).toBeGreaterThan(iUserRole);
  expect(iAssistantRole).toBeGreaterThan(iUser);
  expect(iAssistant).toBeGreaterThan(iAssistantRole);
});
