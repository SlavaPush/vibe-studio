import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

test("кнопка «Выйти» завершает сессию и возвращает на экран входа", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");

  const logout = page.getByRole("button", { name: "Выйти", exact: true });
  const loginHeading = page.getByRole("heading", { name: "Вход в vibe-chat", exact: true });
  const composer = page.getByLabel("Сообщение", { exact: true });

  // 1. После входа в основном интерфейсе видна кнопка «Выйти»
  await expect(page.getByRole("button", { name: fixtures.conversation.title, exact: true })).toBeVisible();
  await expect(composer).toBeVisible();
  await expect(logout).toBeVisible();
  await expect(loginHeading).toHaveCount(0);

  // 4. Кнопка доступна с клавиатуры
  await logout.focus();
  await expect(logout).toBeFocused();
  await page.keyboard.press("Enter");

  // 2. Переход на экран входа
  await expect(loginHeading).toBeVisible();
  await expect(page.getByLabel("Код доступа", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Войти", exact: true })).toBeVisible();

  // 3. Экран чата больше не отображается
  await expect(composer).toHaveCount(0);
  await expect(page.getByRole("button", { name: fixtures.conversation.title, exact: true })).toHaveCount(0);

  // 5. На экране входа кнопки «Выйти» нет
  await expect(logout).toHaveCount(0);
});
