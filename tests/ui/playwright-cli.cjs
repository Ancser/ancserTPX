const { spawnSync } = require("node:child_process");
const path = require("node:path");

const cli = path.join(
  path.dirname(require.resolve("playwright/package.json")),
  "cli.js",
);
const result = spawnSync(process.execPath, [cli, ...process.argv.slice(2)], {
  cwd: process.cwd(),
  env: {
    ...process.env,
    PLAYWRIGHT_BROWSERS_PATH: path.resolve(".playwright-browsers"),
  },
  stdio: "inherit",
});

if (result.error) throw result.error;
process.exit(result.status ?? 1);
