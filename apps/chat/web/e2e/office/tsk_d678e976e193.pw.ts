import { test, expect } from "@playwright/test";
import { mockApi, fixtures } from "./support/mock-api";

test("кошка появляется раз в минуту, собака гонится за ней и возвращается", async ({ page }) => {
  test.setTimeout(240000);
  await mockApi(page);
  await page.clock.install();
  await page.goto("/");
  await expect(page.getByRole("button", { name: fixtures.conversation.title, exact: true })).toBeVisible();

  let t = 0;
  const run = async (ms: number) => {
    await page.clock.runFor(ms);
    t += ms;
  };
  const dog = page.getByTestId("dog-follower");
  const cat = page.getByTestId("cat-runner");
  const vp = page.viewportSize() ?? { width: 1280, height: 720 };
  const num = async (loc: typeof dog, name: string) => Number(await loc.getAttribute(name));
  const edgeOf = (x: number, y: number) => {
    const d: [string, number][] = [
      ["left", x],
      ["right", vp.width - x],
      ["top", y],
      ["bottom", vp.height - y],
    ];
    d.sort((a, b) => a[1] - b[1]);
    return d[0][0];
  };
  const opposite: Record<string, string> = { left: "right", right: "left", top: "bottom", bottom: "top" };

  // Критерий 1: собака следует за курсором, кошки нет
  await expect(dog).toBeVisible();
  await page.mouse.move(600, 400);
  await run(1000);
  await page.mouse.move(300, 250);
  await run(1000);
  await expect(cat).toHaveCount(0);
  await expect.poll(async () => num(dog, "data-distance")).toBeLessThan(90);
  await run(30000);
  await expect(cat).toHaveCount(0);

  // Критерий 2: кошка появляется примерно через минуту с края
  let appeared = false;
  for (let i = 0; i < 600; i++) {
    await run(100);
    if ((await cat.count()) > 0) {
      appeared = true;
      break;
    }
  }
  expect(appeared).toBe(true);
  const firstAppear = t;
  expect(firstAppear).toBeGreaterThan(50000);
  await expect(cat).toBeVisible();
  await expect(cat).toHaveAttribute("data-x", /\d/);
  const cx = await num(cat, "data-x");
  const cy = await num(cat, "data-y");
  const catEdge = edgeOf(cx, cy);
  expect(Math.min(cx, vp.width - cx, cy, vp.height - cy)).toBeLessThan(100);
  const startDist = await num(cat, "data-distance");
  expect(startDist).toBeGreaterThan(150);
  await run(300);
  expect(await num(cat, "data-distance")).toBeLessThan(startDist + 1);

  // Критерий 3: кошка останавливается в 100-150 px от собаки
  let stopped = false;
  for (let i = 0; i < 200; i++) {
    const d = await num(cat, "data-distance");
    if (d <= 150.5) {
      stopped = true;
      break;
    }
    await run(100);
  }
  expect(stopped).toBe(true);
  const stopDist = await num(cat, "data-distance");
  expect(stopDist).toBeGreaterThanOrEqual(99.5);
  expect(stopDist).toBeLessThanOrEqual(150.5);
  await run(100);
  const stopDist2 = await num(cat, "data-distance");
  expect(stopDist2).toBeGreaterThanOrEqual(99.5);
  expect(stopDist2).toBeLessThanOrEqual(150.5);

  // собака бросается в погоню, обе скрываются
  let away = false;
  for (let i = 0; i < 400; i++) {
    await run(100);
    if ((await dog.getAttribute("data-scene")) === "away") {
      away = true;
      break;
    }
  }
  expect(away).toBe(true);
  await expect(dog).toBeHidden();
  await expect(cat).toHaveCount(0);
  const awayAt = t;

  // Критерий 4: через 10 секунд собака возвращается с противоположного края
  await run(9000);
  await expect(dog).toBeHidden();
  let returned = false;
  for (let i = 0; i < 200; i++) {
    await run(16);
    if ((await dog.getAttribute("data-scene")) === "return") {
      returned = true;
      break;
    }
  }
  expect(returned).toBe(true);
  expect(t - awayAt).toBeGreaterThanOrEqual(9900);
  expect(t - awayAt).toBeLessThanOrEqual(11000);
  await expect(dog).toBeVisible();
  const dx = await num(dog, "data-x");
  const dy = await num(dog, "data-y");
  expect(edgeOf(dx, dy)).toBe(opposite[catEdge]);
  await page.mouse.move(700, 350);
  await run(3000);
  await expect.poll(async () => num(dog, "data-distance")).toBeLessThan(90);
  await expect(dog).toBeVisible();
  await expect(cat).toHaveCount(0);

  // Критерий 5: сценарий повторяется раз в минуту
  let again = false;
  for (let i = 0; i < 700; i++) {
    await run(100);
    if ((await cat.count()) > 0) {
      again = true;
      break;
    }
  }
  expect(again).toBe(true);
  const secondAppear = t;
  expect(secondAppear - firstAppear).toBeGreaterThanOrEqual(58000);
  expect(secondAppear - firstAppear).toBeLessThanOrEqual(62500);
  await expect(cat).toBeVisible();
});
