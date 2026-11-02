// The real dashboard server over local HTTP on a TEMP accounts file, driven by a real browser: edit, save, reload.
// Run: ROUTER_MODELS_JSON=<a real router's /v1/models body> node tests/account_ui_live_test.mjs
// Env: PLAYWRIGHT_DIR (default ~/Projects/gittok), CHROMIUM.
import assert from "node:assert/strict";
import { once } from "node:events";
import { createInterface } from "node:readline";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const { chromium } = createRequire(join(process.env.PLAYWRIGHT_DIR || join(homedir(), "Projects/gittok"), "x.js"))("playwright");
assert(process.env.ROUTER_MODELS_JSON, "set ROUTER_MODELS_JSON");
const dir = mkdtempSync(join(tmpdir(), "ui-live-")), file = join(dir, "accounts.json");
writeFileSync(file, JSON.stringify({ main: "a@example.com", accounts: { "a@example.com": {} }, renamed: { old: "a@example.com" } }));
const server = spawn("python3", ["-I", join(here, "account_ui_serve_fixture.py"), file, "0", process.env.ROUTER_MODELS_JSON], { stdio: ["ignore", "pipe", "inherit"] });
let browser;
try {
  const lines = createInterface({input: server.stdout});
  const [ready] = await Promise.race([
    once(lines, "line", {signal: AbortSignal.timeout(10000)}),
    once(server, "exit").then(([code]) => { throw new Error(`dashboard fixture exited ${code}`); }),
  ]);
  lines.close();
  const port = Number(ready);
  assert(Number.isInteger(port) && port > 0, "fixture reports its bound port");
  browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM || undefined });
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${port}/#models`);
  await page.locator("#models article").nth(2).waitFor();
  const real = JSON.parse(readFileSync(process.env.ROUTER_MODELS_JSON)).fallback_defaults.tiers;
  assert.equal(await page.locator("#models article").count(), real.length, "the editor starts from the router's own groups");
  assert.equal(await page.locator('#models [data-f="match"]').first().inputValue(), real[0].match.join(", "));
  const a = page.locator("#models article").first();
  await a.locator('[data-m="codex"]').fill("sol[1m]");
  await a.locator('[data-m="anthropic"]').fill("claude-opus-5-5");
  await page.locator("#models [data-save]").click();
  await page.waitForFunction(() => document.querySelector("#models [data-save]").disabled && !document.querySelector("#models [data-reset]").disabled);
  const onDisk = JSON.parse(readFileSync(file, "utf8"));
  assert.equal(onDisk.fallback.tiers.length, real.length, "saving writes the whole list");
  assert.equal(onDisk.fallback.tiers[0].models.codex, "sol[1m]");
  assert.deepEqual(onDisk.renamed, { old: "a@example.com" }, "unrelated config untouched");
  await page.reload();
  await page.locator("#models article").first().waitFor();
  assert.equal(await page.locator('#models [data-m="codex"]').first().inputValue(), "sol[1m]", "reload shows the saved equivalence");
  await page.locator("#models [data-reset]").click();
  await page.waitForFunction(() => document.querySelector("#models [data-reset]").disabled);
  assert.equal("tiers" in (JSON.parse(readFileSync(file, "utf8")).fallback ?? {}), false, "reset removes the override");
  assert.equal(await page.locator('#models [data-m="codex"]').first().inputValue(), real[0].models.codex);
  console.log("ok live save reload reset");
} finally {
  await browser?.close();
  server.kill();
}
