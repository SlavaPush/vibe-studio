import { test, expect } from "@playwright/test";

const USER_ID = "7b0c2f3e-5d1a-4c8e-9f21-3a6d8e4b1c90";
const CONV_ID = "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37";
const HINT = "Enter — отправить, Shift+Enter — новая строка";
const CSRF = "b4f1c9e27d3a48e6a0c5f8d1e2b3a4c5";

const session = { user_id: USER_ID, expires_at: "2026-10-30T18:00:00Z", csrf_token: CSRF };
const catalog = {
  items: [
    { id: "claude-haiku-5-5", provider: "anthropic", label: "Claude Haiku 5.5", kind: "text", selectable: true },
    { id: "claude-sonnet-5-5", provider: "anthropic", label: "Claude Sonnet 5.5", kind: "text", selectable: true },
    { id: "gpt-6-luna", provider: "openai", label: "GPT-6 Luna", kind: "text", selectable: true },
    { id: "gpt-6.1-sol", provider: "openai", label: "GPT-6.1 Sol", kind: "text", selectable: true },
    { id: "gpt-image-2.5-flare", provider: "openai", label: "GPT Image 2.5 Flare", kind: "image", selectable: false },
  ],
};

test.describe("подсказка горячих клавиш в композере", () => {
  test.beforeEach(async ({ page }) => {
    let signedIn = false;
    await page.route("**/api/**", async (route) => {
      const req = route.request();
      const method = req.method();
      const path = new URL(req.url()).pathname;
      const json = (status: number, body: unknown) =>
        route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

      if (method === "GET" && path === "/api/version") return json(200, { version: "4bd1e4f" });
      if (method === "GET" && path === "/api/models") return json(200, catalog);
      if (method === "GET" && path === "/api/me") {
        return signedIn
          ? json(200, session)
          : json(401, { code: "unauthorized", message: "Сеанс не найден или истёк. Введите код доступа.", retryable: false, operation_id: "op_me" });
      }
      if (method === "POST" && path === "/api/demo-session") {
        signedIn = true;
        return json(201, session);
      }
      if (method === "GET" && path === "/api/conversations") {
        return json(200, {
          items: [
            { id: CONV_ID, title: "Кот в скафандре", created_at: "2026-10-23T18:29:40Z", updated_at: "2026-10-23T18:30:12Z" },
          ],
        });
      }
      if (method === "POST" && path === "/api/conversations") {
        return json(201, { id: CONV_ID, title: "Новый диалог", created_at: "2026-10-23T17:05:00Z", updated_at: "2026-10-23T17:05:00Z" });
      }
      if (method === "GET" && path === `/api/conversations/${CONV_ID}/messages`) {
        return json(200, { items: [], active_run_id: null, runs: [] });
      }
      if (method === "POST" && path === `/api/conversations/${CONV_ID}/runs`) {
        return json(429, { code: "rate_limited", message: "Дождитесь завершения текущего ответа.", retryable: true, operation_id: "op_run" });
      }
      return json(404, { code: "not_found", message: "Не найдено.", retryable: false, operation_id: "op_default" });
    });
  });

  test("подсказка видна только при открытом диалоге, мелкая и серая, не мешает вводу и Enter/Shift+Enter работают", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Код доступа").fill("demo-2026");
    await page.getByRole("button", { name: "Войти" }).click();

    // Стартовый экран без выбранного диалога: подсказки нет.
    await expect(page.getByRole("heading", { name: "vibe-chat" })).toBeVisible();
    await expect(page.getByText(HINT)).toHaveCount(0);

    // Открываем диалог.
    await page.getByRole("button", { name: "Кот в скафандре" }).click();
    const hint = page.getByText(HINT);
    await expect(hint).toBeVisible();

    // Подсказка расположена под полем ввода.
    const input = page.getByLabel("Сообщение");
    const inputBox = await input.boundingBox();
    const hintBox = await hint.boundingBox();
    expect(inputBox).not.toBeNull();
    expect(hintBox).not.toBeNull();
    expect(hintBox!.y).toBeGreaterThanOrEqual(inputBox!.y + inputBox!.height);

    // Мелкий размер и серый цвет, отличающийся от основного текста.
    const hintStyle = await hint.evaluate((el) => {
      const s = getComputedStyle(el);
      return { fontSize: parseFloat(s.fontSize), color: s.color };
    });
    const bodyStyle = await page.locator("main h1").evaluate((el) => ({
      fontSize: parseFloat(getComputedStyle(el).fontSize),
      color: getComputedStyle(el).color,
    }));
    expect(hintStyle.fontSize).toBeLessThan(bodyStyle.fontSize);
    expect(hintStyle.color).not.toBe(bodyStyle.color);

    // Подсказка не блокирует поле и кнопку отправки.
    await expect(input).toBeEnabled();
    await expect(hint).toHaveCSS("pointer-events", "none");
    await input.fill("Привет");
    await expect(input).toHaveValue("Привет");
    await expect(page.getByRole("button", { name: "Отправить" })).toBeEnabled();

    // Shift+Enter добавляет новую строку, Enter отправляет.
    await input.fill("первая");
    await input.press("Shift+Enter");
    await input.pressSequentially("вторая");
    await expect(input).toHaveValue("первая\nвторая");

    await input.fill("отправка");
    const runRequest = page.waitForRequest(
      (req) => req.method() === "POST" && new URL(req.url()).pathname === `/api/conversations/${CONV_ID}/runs`,
    );
    await input.press("Enter");
    await runRequest;
  });
});
