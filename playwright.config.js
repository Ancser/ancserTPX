const path = require("node:path");
const { defineConfig, devices } = require("@playwright/test");

const host = "127.0.0.1";
const port = 8765;
const baseURL = `http://${host}:${port}`;

module.exports = defineConfig({
  testDir: "./tests/ui",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: "list",
  outputDir: path.resolve(".playwright-output"),
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    headless: true,
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "off",
    video: "off",
  },
  webServer: {
    command: `python -m uvicorn backend.main:app --host ${host} --port ${port} --lifespan off`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 30_000,
    stdout: "pipe",
    stderr: "pipe",
    env: {
      PYTHONUNBUFFERED: "1",
      TOPSTEPX_USERNAME: "",
      TOPSTEPX_API_KEY: "",
      DISCORD_TOKEN: "",
    },
  },
});
