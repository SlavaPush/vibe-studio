import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

test("собака бежит за курсором, не догоняет и садится при простое", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: fixtures.conversation.title, exact: true }).click();
  await expect(page.getByLabel("Сообщение", { exact: true })).toBeVisible();

  const dog = page.getByTestId("dog-follower");
  await expect(dog).toBeVisible();

  const cx = 300;
  const cy = 200;
  for (let i = 0; i < 70; i++) {
    await page.mouse.move(cx + (i % 2) * 2, cy);
    await page.waitForTimeout(20);
  }

  await expect(dog).toHaveAttribute("data-state", "running");
  await expect(dog.locator('[data-asset="dog-running"]')).toBeVisible();

  const x = Number(await dog.getAttribute("data-x"));
  const y = Number(await dog.getAttribute("data-y"));
  const dist = Math.hypot(x - (cx + 1), y - cy);
  expect(dist).toBeGreaterThan(5);
  expect(dist).toBeLessThan(200);

  // курсор замер: собака садится и думает
  await expect(dog).toHaveAttribute("data-state", "sitting", { timeout: 5000 });
  await expect(dog.locator('[data-asset="dog-thinking"]')).toBeVisible();

  // движение возобновилось: снова бежит
  await page.mouse.move(600, 400, { steps: 5 });
  await expect(dog).toHaveAttribute("data-state", "running");
  await expect(dog.locator('[data-asset="dog-running"]')).toBeVisible();

  // интерфейс чата по-прежнему работает
  await expect(page.getByLabel("Сообщение", { exact: true })).toBeVisible();
});
