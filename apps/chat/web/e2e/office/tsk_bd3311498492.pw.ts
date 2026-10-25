import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

test("заголовок вкладки отражает активный диалог", async ({ page }) => {
  let created = 0;
  await mockApi(page, {
    "GET /api/conversations": { body: { items: [] } },
    "POST /api/conversations": () => {
      created += 1;
      return { status: 201, body: created === 1 ? fixtures.newConversation : fixtures.conversation };
    },
    "PATCH /api/conversations/{id}": ({ params, body }) => {
      const known = [fixtures.conversation, fixtures.newConversation].find((c) => c.id === params.id)!;
      const title = (body as { title?: string }).title ?? known.title;
      return { body: { ...known, title } };
    },
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Новый диалог", exact: true }).first()).toBeVisible();
  await expect(page).toHaveTitle("vibe-chat");

  await page.getByRole("button", { name: "Новый диалог", exact: true }).first().click();
  await expect(page).toHaveTitle("Новый диалог — vibe-chat");

  await page.getByRole("button", { name: "Новый диалог", exact: true }).first().click();
  await expect(page).toHaveTitle(`${fixtures.conversation.title} — vibe-chat`);

  await page.getByRole("listitem").getByRole("button", { name: "Новый диалог", exact: true }).click();
  await expect(page).toHaveTitle("Новый диалог — vibe-chat");

  await page.getByRole("button", { name: "Переименовать диалог «Новый диалог»", exact: true }).click();
  const input = page.getByLabel("Название диалога", { exact: true });
  await input.fill("Тест");
  await input.press("Enter");
  await expect(page).toHaveTitle("Тест — vibe-chat");
});
