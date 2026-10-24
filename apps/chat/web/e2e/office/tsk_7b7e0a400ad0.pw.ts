import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

const NEW_PLACEHOLDER = "Спросите что-нибудь…";
const OLD_PLACEHOLDER = "Напишите сообщение";

test("заполнитель поля ввода сообщения заменён", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: fixtures.conversation.title, exact: true }).click();

  const input = page.getByLabel("Сообщение", { exact: true });
  await expect(input).toBeVisible();

  // Критерии 1 и 2: пустое поле
  await expect(input).toHaveValue("");
  await expect(input).toHaveAttribute("placeholder", NEW_PLACEHOLDER);
  await expect(page.getByPlaceholder(NEW_PLACEHOLDER, { exact: true })).toHaveCount(1);
  await expect(page.getByPlaceholder(OLD_PLACEHOLDER, { exact: true })).toHaveCount(0);
  await expect(input).not.toHaveAttribute("placeholder", OLD_PLACEHOLDER);
  expect(await input.evaluate((el) => el.matches(":placeholder-shown"))).toBe(true);

  // Критерий 3: ввод текста
  const text = "Привет, QA";
  await input.fill(text);
  await expect(input).toHaveValue(text);
  await expect
    .poll(() => input.evaluate((el) => el.matches(":placeholder-shown")))
    .toBe(false);
  await expect(input).toHaveAttribute("placeholder", NEW_PLACEHOLDER);

  // Критерий 4: отправка
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  await expect(page.getByText(text, { exact: true })).toBeVisible();
  await expect(page.getByText(fixtures.reply, { exact: true })).toBeVisible();
  await expect(input).toHaveValue("");
  await expect(input).toHaveAttribute("placeholder", NEW_PLACEHOLDER);
  expect(await input.evaluate((el) => el.matches(":placeholder-shown"))).toBe(true);
});
