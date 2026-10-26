import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

const longTitle =
  "Очень длинное название диалога которое точно не поместится в узкий контейнер списка диалогов и будет обрезано многоточием по краю";
const shortTitle = "Короткий";

test("подсказка с полным названием только у обрезанных названий диалогов", async ({ page }) => {
  const longConv = { ...fixtures.conversation, title: longTitle };
  const shortConv = {
    ...fixtures.newConversation,
    title: shortTitle,
  };
  await mockApi(page, {
    "GET /api/conversations": { body: { items: [longConv, shortConv] } },
  });
  await page.goto("/");

  const longButton = page.getByRole("button", { name: longTitle, exact: true });
  const shortButton = page.getByRole("button", { name: shortTitle, exact: true });
  await expect(longButton).toBeVisible();
  await expect(shortButton).toBeVisible();

  const longText = longButton.getByText(longTitle, { exact: true });
  await longText.hover();
  await expect(longText).toHaveAttribute("title", longTitle);

  const shortText = shortButton.getByText(shortTitle, { exact: true });
  await shortText.hover();
  await expect(shortText).not.toHaveAttribute("title", /.+/);
});
