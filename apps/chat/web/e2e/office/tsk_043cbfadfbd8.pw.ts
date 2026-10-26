import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

const longTitle =
  "Очень длинное название диалога которое точно не поместится в боковую панель списка диалогов и будет обрезано многоточием";

test("длинное название диалога обрезается и имеет title с полным названием", async ({ page }) => {
  const longConv = {
    id: "11111111-2222-4333-8444-555555555555",
    title: longTitle,
    created_at: "2026-10-23T18:00:00Z",
    updated_at: "2026-10-23T18:00:00Z",
  };
  await mockApi(page, {
    "GET /api/conversations": { body: { items: [longConv, fixtures.conversation] } },
  });
  await page.goto("/");

  const nav = page.getByRole("navigation", { name: "Диалоги", exact: true });
  const longButton = nav.getByRole("button", { name: longTitle, exact: true });
  await expect(longButton).toBeVisible();
  const longSpan = longButton.locator("span").first();
  await expect(longSpan).toHaveAttribute("title", longTitle);
  await expect(longSpan).toHaveCSS("text-overflow", "ellipsis");
  await expect(longSpan).toHaveCSS("white-space", "nowrap");
  const truncated = await longSpan.evaluate((el) => el.scrollWidth > el.clientWidth);
  expect(truncated).toBe(true);

  const shortButton = nav.getByRole("button", { name: fixtures.conversation.title, exact: true });
  await expect(shortButton).toBeVisible();
  const shortSpan = shortButton.locator("span").first();
  await expect(shortSpan).toHaveText(fixtures.conversation.title);
  await expect(shortSpan).toHaveAttribute("title", fixtures.conversation.title);
  const shortTruncated = await shortSpan.evaluate((el) => el.scrollWidth > el.clientWidth);
  expect(shortTruncated).toBe(false);

  await shortButton.click();
  await expect(shortButton).toHaveAttribute("aria-current", "page");
  await expect(page.getByLabel("Сообщение", { exact: true })).toBeVisible();
});
