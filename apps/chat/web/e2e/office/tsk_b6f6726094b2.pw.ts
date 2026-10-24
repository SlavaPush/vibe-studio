import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

const HINT = "Enter — отправить, Shift+Enter — новая строка";

test("подсказка под полем ввода при открытом диалоге", async ({ page }) => {
  // Критерий 3: нет открытого диалога (пустой список) — подсказки нет, поле ввода есть
  await mockApi(page, { "GET /api/conversations": { body: { items: [] } } });
  await page.goto("/");
  const input = page.getByLabel("Сообщение", { exact: true });
  const hint = page.getByText(HINT, { exact: true });
  await expect(input).toBeVisible();
  await expect(hint).toHaveCount(0);

  // Открытый диалог: обычная подмена, перезагрузка
  await mockApi(page);
  await page.reload();
  const dialogButton = page.getByRole("button", { name: fixtures.conversation.title, exact: true });
  await expect(dialogButton).toBeVisible();
  await dialogButton.click();

  // Критерий 1: подсказка видна
  await expect(hint).toBeVisible();
  await expect(input).toBeVisible();

  // Критерий 4: подсказка под полем ввода и не перекрывает поле и кнопку отправки
  const inputBox = await input.boundingBox();
  const hintBox = await hint.boundingBox();
  const sendBox = await page.getByRole("button", { name: "Отправить", exact: true }).boundingBox();
  expect(inputBox).not.toBeNull();
  expect(hintBox).not.toBeNull();
  expect(sendBox).not.toBeNull();
  expect(hintBox!.y).toBeGreaterThanOrEqual(inputBox!.y + inputBox!.height - 1);
  expect(hintBox!.y).toBeGreaterThanOrEqual(sendBox!.y + sendBox!.height - 1);

  // Критерий 2: мелкий вспомогательный текст другого цвета, чем у поля ввода
  const hintStyle = await hint.evaluate((el) => {
    const s = getComputedStyle(el);
    return { size: parseFloat(s.fontSize), color: s.color };
  });
  const inputStyle = await input.evaluate((el) => {
    const s = getComputedStyle(el);
    return { size: parseFloat(s.fontSize), color: s.color };
  });
  expect(hintStyle.size).toBeLessThanOrEqual(13);
  expect(hintStyle.size).toBeLessThan(inputStyle.size);
  expect(hintStyle.color).not.toBe(inputStyle.color);

  // Критерий 5: Shift+Enter добавляет строку и не отправляет
  await input.fill("строка1");
  await input.press("Shift+Enter");
  await input.pressSequentially("строка2");
  await expect(input).toHaveValue("строка1\nстрока2");

  // Enter отправляет сообщение
  const runRequest = page.waitForRequest(
    (request) => request.method() === "POST" && /\/api\/conversations\/[^/]+\/runs$/.test(new URL(request.url()).pathname),
  );
  await input.press("Enter");
  const sent = await runRequest;
  expect((sent.postDataJSON() as { text: string }).text).toBe("строка1\nстрока2");
  await expect(page.getByText(fixtures.reply, { exact: true })).toBeVisible();
  await expect(input).toHaveValue("");
  await expect(hint).toBeVisible();
});
