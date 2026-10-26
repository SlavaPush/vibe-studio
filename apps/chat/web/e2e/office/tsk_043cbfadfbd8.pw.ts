import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

test("у названий диалогов есть подсказка title с полным названием", async ({ page }) => {
  const longTitle =
    "Очень длинное название диалога которое точно не помещается в боковую панель списка диалогов и должно быть обрезано многоточием";
  const shortTitle = "Короткий";
  const longConv = { ...fixtures.conversation, title: longTitle };
  const shortConv = {
    ...fixtures.newConversation,
    title: shortTitle,
  };
  await mockApi(page, {
    "GET /api/conversations": { body: { items: [longConv, shortConv] } },
  });
  await page.goto("/");

  const nav = page.getByRole("navigation", { name: "Диалоги", exact: true });
  const longButton = nav.getByRole("button", { name: longTitle, exact: true });
  const shortButton = nav.getByRole("button", { name: shortTitle, exact: true });
  await expect(longButton).toBeVisible();
  await expect(shortButton).toBeVisible();

  const longName = nav.locator(`[title="${longTitle}"]`);
  await expect(longName).toHaveCount(1);
  await expect(longName).toHaveText(longTitle);
  await expect(longName).toHaveAttribute("title", longTitle);
  await expect(longName).toHaveCSS("text-overflow", "ellipsis");
  await expect(longName).toHaveCSS("white-space", "nowrap");
  await expect(longName).toHaveCSS("overflow", "hidden");

  await longName.hover();
  await expect(longName).toHaveAttribute("title", longTitle);

  const shortName = nav.locator(`[title="${shortTitle}"]`);
  await expect(shortName).toHaveCount(1);
  await expect(shortName).toHaveText(shortTitle);

  await shortButton.click();
  await expect(shortButton).toHaveAttribute("aria-current", "page");
});
