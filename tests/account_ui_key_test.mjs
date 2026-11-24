// Real-DOM check: a Console API key shows as a fixed card above the draggable accounts, toggles on/off,
// and never enters the account `order` or the preferred-account picker.
// Run: node tests/account_ui_key_test.mjs (same PLAYWRIGHT_DIR / CHROMIUM env as account_ui_drag_test.mjs).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.env.PLAYWRIGHT_DIR || join(homedir(), "Projects/gittok");
const { chromium } = createRequire(join(root, "x.js"))("playwright");
const html = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../scripts/account_ui.html"), "utf8");

const KEY = { alias: "console-key", kind: "api_key", preferred: true, slot: null, email: null, plan: "api key",
  usage: {}, usage_at: null, token_only: false, error: null, disabled: false, spend_usd: 150.5, budget_usd: 180 };
const ACCOUNTS = [
  { alias: "a@example.com", email: "a@example.com", slot: 1, usage: {} },
  { alias: "b@example.com", email: "b@example.com", slot: 2, usage: {} },
  KEY,
];

const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM || undefined });
try {
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 2400 } })).newPage();
  const posts = [];
  let accounts = structuredClone(ACCOUNTS);
  const state = () => ({
    accounts, balance: { strategy: "prefer", prefer: "a@example.com" }, strategies: ["session", "prefer"], fallback: {},
    fallback_defaults: { chains: { claude: ["accounts", "codex"] }, tiers: [] },
    providers: { reachable: true, models: { codex: ["gpt-6.1-sol"] }, catalog: { codex: { label: "Codex", family: "codex", models: ["gpt-6.1-sol"] } } },
    proxy: { address: "x:1", up: true }, db: { path: null, bytes: 0, disk_free: 1 }, traffic_days: null,
  });
  await page.route("http://dash.test/**", (route) => {
    const req = route.request(), url = new URL(req.url());
    const json = (body) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    if (url.pathname === "/api/config") {
      const change = req.postDataJSON();
      posts.push(change);
      if (change.order) accounts = [...change.order.map((alias) => accounts.find((a) => a.alias === alias)), KEY];
      if (change.disabled) accounts = accounts.map((a) => ({ ...a, disabled: change.disabled.includes(a.alias) }));
      return json(state());
    }
    if (url.pathname === "/api/state") return json(state());
    if (url.pathname === "/api/stats") return json({ total: 0, errors: 0, series: {}, by_account: {}, by_host: {}, by_model: {} });
    if (url.pathname === "/api/sessions") return json({ days: 7, limit: 50, traffic_db: false, sessions: [] });
    return json({ rows: [] });
  });
  await page.goto("http://dash.test/#accounts");
  await page.locator("#accounts .card").first().waitFor();

  const first = page.locator("#accounts .card").first();
  assert.match(await first.innerText(), /console-key[\s\S]*tried first[\s\S]*api key/);
  assert.equal(await first.locator(".handle").count(), 0, "the key card is not draggable");
  assert.match(await first.innerText(), /84%[\s\S]*\$150\.50 of \$180\.00 this month/, "spend against the budget");
  assert.equal(await page.locator('#accounts .card[data-i] .name').allTextContents().then((n) => n.includes("console-key")), false);

  await page.locator("#accounts .card[data-i]").nth(1).locator("[data-up]").click();
  await page.waitForFunction(() => document.querySelectorAll("#accounts .card").length > 0);
  assert.deepEqual(posts[0].order, ["b@example.com", "a@example.com"], "the key never enters the account order");

  await first.locator(".switch span").click();
  await page.waitForFunction(() => document.querySelector("#accounts .card").classList.contains("off"));
  assert.deepEqual(posts[1].disabled, ["console-key"], "the switch turns the key off");

  await page.goto("http://dash.test/#routing");
  const options = await page.locator("#prefer option").allTextContents();
  assert.deepEqual(options, ["b@example.com", "a@example.com"], "only subscriptions can be the preferred account");
  console.log("account_ui_key_test: ok");
} finally {
  await browser.close();
}
