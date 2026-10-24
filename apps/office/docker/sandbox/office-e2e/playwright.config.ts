import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "/qa",
  testMatch: "**/*.pw.ts",
  outputDir: "/out/test-results",
  reporter: [["line"], ["json", { outputFile: "/out/playwright-report.json" }]],
  forbidOnly: true,
  retries: 0,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:4173",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node /office-e2e/serve.mjs",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
