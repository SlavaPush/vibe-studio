import { test, expect } from "@playwright/test";
const HINT = "Enter — отправить, Shift+Enter — новая строка";
const CONV_ID = "5f0e8a52-3c1d-4b7e-9a10-6c2d4e8f1a37";
const RUN_ID = "0b8d6f24-7a3e-4c91-b2d5-8e6f1a3c9d47";
const SESSION = { user_id: "7b0c2f3e-5d1a-4c8e-9f21-3a6d8e4b1c90", expires_at: "2026-10-30T18:00:00Z", csrf_token: "b4f1c9e27d3a48e6a0c5f8d1e2b3a4c5" };
const CONVERSATION = { id: CONV_ID, title: "Кот в скафандре", created_at: "2026-10-23T18:29:40Z", updated_at: "2026-10-23T18:30:12Z" };
const MODELS = { items: [{ id: "claude-sonnet-5-5", provider: "anthropic", label: "Claude Sonnet 5.5", kind: "text", selectable: true }] };
const RUN = { id: RUN_ID, conversation_id: CONV_ID, status: "queued", requested_model: "claude-sonnet-5-5", effective_model: null, effort: null, intent: null, complexity: null, risk: null, switch_reason: null, user_message_id: "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f", assistant_message_id: null, error: null, created_at: "2026-10-23T18:30:00Z", updated_at: "2026-10-23T18:30:00Z" };
const MESSAGES = { items: [], active_run_id: null, runs: [] };

test("подсказка под полем ввода сообщения", async ({ page }) => {
  const runBodies: string[] = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (method === "GET" && path === "/api/me") return json(SESSION);
    if (method === "GET" && path === "/api/version") return json({ version: "test" });
    if (method === "GET" && path === "/api/models") return json(MODELS);
    if (method === "GET" && path === "/api/conversations") return json({ items: [CONVERSATION] });
    if (method === "GET" && path === `/api/conversations/${CONV_ID}/messages`) return json(MESSAGES);
    if (method === "POST" && path === `/api/conversations/${CONV_ID}/runs`) { runBodies.push(request.postData() ?? ""); return json(RUN, 202); }
    if (method === "GET" && path === `/api/runs/${RUN_ID}/events`) return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
    return json({ code: "not_found", message: "Не найдено.", retryable: false, operation_id: "op_test" }, 404);
  });

  await page.goto("/");
  const input = page.getByLabel("Сообщение");
  await expect(input).toBeVisible();
  await expect(page.getByText(HINT)).toHaveCount(0);

  await page.getByRole("button", { name: "Кот в скафандре" }).click();
  const hint = page.getByText(HINT);
  await expect(hint).toBeVisible();

  const hintStyle = await hint.evaluate((el) => { const s = getComputedStyle(el); return { fontSize: parseFloat(s.fontSize), color: s.color }; });
  const inputFont = await input.evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
  const titleColor = await page.getByRole("heading", { level: 1 }).evaluate((el) => getComputedStyle(el).color);
  expect(hintStyle.fontSize).toBeLessThan(inputFont);
  expect(hintStyle.color).not.toBe(titleColor);

  const inputBox = await input.boundingBox();
  const hintBox = await hint.boundingBox();
  expect(inputBox).not.toBeNull();
  expect(hintBox).not.toBeNull();
  expect(hintBox!.y).toBeGreaterThanOrEqual(inputBox!.y + inputBox!.height - 1);
  await input.fill("привет");
  await expect(input).toHaveValue("привет");
  await expect(page.getByRole("button", { name: "Отправить" })).toBeVisible();

  await input.fill("строка 1");
  await input.press("Shift+Enter");
  await input.pressSequentially("строка 2");
  const multiline = await input.inputValue();
  expect(multiline.includes(String.fromCharCode(10))).toBe(true);

  await input.fill("Нарисуй кота");
  await input.press("Enter");
  await expect.poll(() => runBodies.length).toBe(1);
  expect(JSON.parse(runBodies[0]).text).toBe("Нарисуй кота");
});
