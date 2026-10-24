import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

const HINT = "Enter — отправить, Shift+Enter — новая строка";

test("подсказка под полем ввода в открытом диалоге", async ({ page }) => {
  // Критерий 3: диалог не открыт (список диалогов пуст)
  await mockApi(page, { "GET /api/conversations": { body: { items: [] } } });
  await page.goto("/");
  const input = page.getByLabel("Сообщение", { exact: true });
  await expect(input).toBeVisible();
  await expect(page.getByText(HINT, { exact: true })).toHaveCount(0);

  // Открываем диалог: создание нового диалога (более поздняя подмена имеет приоритет)
  await mockApi(page);
  await page.getByRole("button", { name: "Новый диалог", exact: true }).click();

  // Критерий 1: подсказка видна
  const hint = page.getByText(HINT, { exact: true });
  await expect(hint).toBeVisible();
  await expect(input).toBeVisible();

  // Критерий 1: подсказка расположена под полем ввода
  const inputBox = await input.boundingBox();
  const hintBox = await hint.boundingBox();
  expect(inputBox).not.toBeNull();
  expect(hintBox).not.toBeNull();
  expect(hintBox!.y).toBeGreaterThanOrEqual(inputBox!.y + inputBox!.height - 1);

  // Критерий 2: мелкий серый шрифт
  const styles = await hint.evaluate((el) => {
    const hintStyle = getComputedStyle(el);
    const inputEl = document.querySelector('textarea[aria-label="Сообщение"]') as HTMLElement;
    const inputStyle = getComputedStyle(inputEl);
    return {
      hintSize: parseFloat(hintStyle.fontSize),
      inputSize: parseFloat(inputStyle.fontSize),
      hintColor: hintStyle.color,
      inputColor: inputStyle.color,
    };
  });
  expect(styles.hintSize).toBeLessThanOrEqual(13);
  expect(styles.hintSize).toBeLessThan(styles.inputSize);
  expect(styles.hintColor).not.toBe(styles.inputColor);

  // Критерий 4: подсказка не мешает вводу и не закрывает кнопку отправки
  const sendButton = page.getByRole("button", { name: "Отправить", exact: true });
  await expect(sendButton).toBeVisible();
  const sendBox = await sendButton.boundingBox();
  expect(sendBox).not.toBeNull();
  const overlapsSend =
    hintBox!.x < sendBox!.x + sendBox!.width &&
    hintBox!.x + hintBox!.width > sendBox!.x &&
    hintBox!.y < sendBox!.y + sendBox!.height &&
    hintBox!.y + hintBox!.height > sendBox!.y;
  expect(overlapsSend).toBe(false);
  await input.click();
  await expect(input).toBeFocused();
  await input.pressSequentially("abc");
  await expect(input).toHaveValue("abc");
  const inputBoxAfter = await input.boundingBox();
  expect(inputBoxAfter!.y).toBe(inputBox!.y);
  expect(inputBoxAfter!.x).toBe(inputBox!.x);
  await expect(hint).toBeVisible();
  await input.fill("");

  // Критерий 5: Shift+Enter добавляет новую строку
  await input.click();
  await input.pressSequentially("строка1");
  await input.press("Shift+Enter");
  await input.pressSequentially("строка2");
  await expect(input).toHaveValue("строка1\nстрока2");
  await input.fill("");

  // Критерий 5: Enter отправляет сообщение
  await input.fill("Привет из теста");
  await input.press("Enter");
  await expect(page.getByText("Привет из теста", { exact: true })).toBeVisible();
  await expect(page.getByText(fixtures.reply, { exact: true })).toBeVisible();
  await expect(input).toHaveValue("");
  await expect(hint).toBeVisible();
});
