import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

const conv = fixtures.conversation.id;
function msg(id: string, role: string, text: string) {
  return {
    id,
    conversation_id: conv,
    role,
    kind: null,
    text,
    run_id: null,
    effective_model: role === "assistant" ? fixtures.model : null,
    effort: null,
    asset: null,
    created_at: "2026-10-23T18:30:00Z",
  };
}

test("подсказка кнопки экспорта в markdown", async ({ page }) => {
  await mockApi(page, {
    "GET /api/conversations/{id}/messages": {
      body: {
        items: [
          msg("1c2d3e4f-5a6b-4c7d-8e9f-000000000001", "user", "Привет"),
          msg("1c2d3e4f-5a6b-4c7d-8e9f-000000000002", "assistant", fixtures.reply),
        ],
        active_run_id: null,
        runs: [],
      },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: fixtures.conversation.title, exact: true }).click();
  const button = page.getByRole("button", { name: "Экспорт в Markdown", exact: true });
  await expect(button).toBeVisible();
  await expect(button).toBeEnabled();
  const tooltip = page.getByRole("tooltip");
  await expect(tooltip).toHaveCount(0);

  // 1, 4, 6: наведение
  await button.hover();
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText(/Markdown/i);
  await expect(tooltip).toContainText(/сохран/i);
  const id = await tooltip.getAttribute("id");
  expect(id).toBeTruthy();
  await expect(button).toHaveAttribute("aria-describedby", id!);
  const b = (await button.boundingBox())!;
  const t = (await tooltip.boundingBox())!;
  const vp = page.viewportSize()!;
  const overlap = b.x < t.x + t.width && t.x < b.x + b.width && b.y < t.y + t.height && t.y < b.y + b.height;
  expect(overlap).toBe(false);
  expect(t.x).toBeGreaterThanOrEqual(0);
  expect(t.y).toBeGreaterThanOrEqual(0);
  expect(t.x + t.width).toBeLessThanOrEqual(vp.width);
  expect(t.y + t.height).toBeLessThanOrEqual(vp.height);

  // 3: уход курсора
  await page.mouse.move(2, 2);
  await expect(tooltip).toHaveCount(0);
  await expect(button).not.toHaveAttribute("aria-describedby", /.+/);

  // 2: фокус клавишей Tab
  for (let i = 0; i < 20; i++) {
    if (await button.evaluate((el) => el === document.activeElement)) break;
    await page.keyboard.press("Tab");
  }
  await expect(button).toBeFocused();
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText(/Markdown/i);
  const id2 = await tooltip.getAttribute("id");
  await expect(button).toHaveAttribute("aria-describedby", id2!);

  // 5: Escape
  await page.keyboard.press("Escape");
  await expect(tooltip).toHaveCount(0);

  // 3: потеря фокуса
  await button.evaluate((el) => (el as HTMLElement).blur());
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(tooltip).toBeVisible();
  await button.evaluate((el) => (el as HTMLElement).blur());
  await expect(tooltip).toHaveCount(0);

  // 7: клик работает
  await button.hover();
  await expect(tooltip).toBeVisible();
  const [download] = await Promise.all([page.waitForEvent("download"), button.click()]);
  expect(download.suggestedFilename()).toMatch(/\.md$/);
});
