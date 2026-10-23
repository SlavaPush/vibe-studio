import { test, expect } from "@playwright/test";

test("собранное приложение открывается в браузере", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#root")).not.toBeEmpty();
});
