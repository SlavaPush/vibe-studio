import { test, expect } from "@playwright/test";
import { mockApi } from "./support/mock-api";

test("кошка с собакой возвращаются через 3 секунды и сидят за решёткой слева от селектора моделей", async ({ page }) => {
  await page.clock.install();
  await mockApi(page);
  await page.goto("/");

  const selector = page.getByLabel("Модель", { exact: true });
  await expect(selector).toBeVisible();
  const follower = page.getByTestId("dog-follower");
  await expect(follower).toBeAttached();

  const sceneName = "Кошка и собака с чемоданом играют в карты за решёткой";
  const scene = page.getByRole("group", { name: sceneName, exact: true });

  // До ухода кошки сцены нет.
  await expect(scene).toHaveCount(0);

  // Ждём минутного появления кошки и её ухода вместе с собакой.
  await page.clock.runFor(59000);
  let away = false;
  for (let i = 0; i < 300 && !away; i++) {
    await page.clock.runFor(100);
    away = (await follower.getAttribute("data-scene")) === "away";
  }
  expect(away).toBe(true);

  // До истечения 3 секунд сцены нет.
  await page.clock.runFor(1500);
  await expect(scene).toHaveCount(0);
  await page.clock.runFor(1000);
  await expect(scene).toHaveCount(0);

  // После 3 секунд сцена появляется.
  await page.clock.runFor(800);
  await expect(scene).toBeVisible();

  const cat = page.getByRole("img", { name: "Кошка с татуировками", exact: true });
  const dog = page.getByRole("img", { name: "Собака", exact: true });
  await expect(cat).toBeVisible();
  await expect(dog).toBeVisible();
  await expect(page.getByRole("img", { name: "Чемодан", exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: "Игральные карты", exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: "Тюремная решётка", exact: true })).toBeVisible();

  // Сцена слева от селектора и на одной линии с ним.
  const sceneBox = await scene.boundingBox();
  const selectorBox = await selector.boundingBox();
  expect(sceneBox).not.toBeNull();
  expect(selectorBox).not.toBeNull();
  const s = sceneBox!;
  const m = selectorBox!;
  expect(s.x).toBeLessThan(m.x);
  expect(s.x + s.width / 2).toBeLessThan(m.x);
  const sceneMid = s.y + s.height / 2;
  expect(sceneMid).toBeGreaterThan(m.y);
  expect(sceneMid).toBeLessThan(m.y + m.height);

  // Кошка крупнее собаки и с татуировками.
  const catBox = (await cat.boundingBox())!;
  const dogBox = (await dog.boundingBox())!;
  expect(catBox.width * catBox.height).toBeGreaterThan(dogBox.width * dogBox.height);
  await expect(page.getByLabel("Татуировки", { exact: true })).toBeVisible();
});
