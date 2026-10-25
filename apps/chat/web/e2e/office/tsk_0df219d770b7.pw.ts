import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

test("счётчик символов под полем ввода", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: fixtures.conversation.title, exact: true }).click();

  const input = page.getByLabel("Сообщение", { exact: true });
  await expect(input).toBeVisible();

  // 1
  const counter = page.getByText("0 / 4000", { exact: true });
  await expect(counter).toBeVisible();

  // 5
  const inputBox = await input.boundingBox();
  const counterBox = await counter.boundingBox();
  expect(inputBox).not.toBeNull();
  expect(counterBox).not.toBeNull();
  expect(counterBox!.y).toBeGreaterThanOrEqual(inputBox!.y + inputBox!.height - 1);

  // 2
  await input.fill("a".repeat(123));
  await expect(page.getByText("123 / 4000", { exact: true })).toBeVisible();

  // 3
  await input.fill("a".repeat(100));
  await expect(page.getByText("100 / 4000", { exact: true })).toBeVisible();
  await input.press("Backspace");
  await expect(page.getByText("99 / 4000", { exact: true })).toBeVisible();

  // 6: узкий экран
  await page.setViewportSize({ width: 320, height: 640 });
  const send = page.getByRole("button", { name: "Отправить", exact: true });
  await expect(page.getByText("99 / 4000", { exact: true })).toBeVisible();
  const c = (await page.getByText("99 / 4000", { exact: true }).boundingBox())!;
  const i = (await input.boundingBox())!;
  const s = (await send.boundingBox())!;
  expect(c.y).toBeGreaterThanOrEqual(i.y + i.height - 1);
  expect(c.y).toBeGreaterThanOrEqual(s.y + s.height - 1);
  expect(c.x).toBeGreaterThanOrEqual(0);
  expect(c.x + c.width).toBeLessThanOrEqual(320);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);

  // 4
  await send.click();
  await expect(page.getByText(fixtures.reply, { exact: true })).toBeVisible();
  await expect(input).toHaveValue("");
  await expect(page.getByText("0 / 4000", { exact: true })).toBeVisible();
});
