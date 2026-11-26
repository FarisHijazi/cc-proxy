// Real-DOM check of each account's "Fail over at" slider: its status text from mocked usage, then a real save
// through the real dashboard server on a TEMP accounts file (no logins, no network, no inference).
// Run: node tests/account_ui_ceiling_test.mjs
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
const html = readFileSync(join(here, "../scripts/account_ui.html"), "utf8");
const soon = new Date(Date.now() + 2 * 3600e3).toISOString(), week = new Date(Date.now() + 5 * 86400e3).toISOString();
const ACCOUNTS = [
  { alias: "over@example.com", email: "over@example.com", slot: 1,
    usage: { five_hour: { utilization: 20, resets_at: soon }, seven_day: { utilization: 5, resets_at: week } } },
  { alias: "under@example.com", email: "under@example.com", slot: 2,
    usage: { five_hour: { utilization: 19, resets_at: soon }, seven_day: { utilization: 10, resets_at: week },
      seven_day_opus: { utilization: 99, resets_at: week } } },
  { alias: "unknown@example.com", email: "unknown@example.com", slot: 3, usage: {} },
  { alias: "loading@example.com", email: "loading@example.com", slot: 4, usage: {}, loading: true },
  { alias: "off@example.com", email: "off@example.com", slot: 5, usage: { five_hour: { utilization: 99, resets_at: soon } } },
];
const state = (failover_at) => ({
  accounts: ACCOUNTS, balance: { strategy: "session" }, strategies: ["session"], fallback: {}, failover_at,
  fallback_defaults: { chains: { claude: ["accounts"] }, tiers: [] },
  providers: { reachable: true, models: {}, catalog: {} },
  proxy: { address: "x:1", up: true }, db: { path: null, bytes: 0, disk_free: 1 }, traffic_days: null,
});
const card = (page, alias) => page.locator("#accounts .card", { hasText: alias });

const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM || undefined });
let server;
try {
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 3000 } })).newPage();
  const ceilings = { "over@example.com": 20, "under@example.com": 20, "unknown@example.com": 50, "loading@example.com": 50 };
  const posts = [];
  let holdSave = null;
  await page.route("http://dash.test/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    if (url.pathname === "/api/config") {
      const change = route.request().postDataJSON();
      posts.push(change);
      if (holdSave) await holdSave;
      Object.assign(ceilings, change.failover_at);
      return json(state(ceilings));
    }
    if (url.pathname === "/api/state") return json(state(ceilings));
    if (url.pathname === "/api/sessions") return json({ days: 7, limit: 50, traffic_db: false, sessions: [] });
    return json({ rows: [], total: 0, errors: 0, series: {}, by_account: {}, by_host: {}, by_model: {} });
  });
  await page.goto("http://dash.test/#accounts");
  await card(page, "off@example.com").waitFor();
  const text = async (alias) => (await card(page, alias).locator(".ceiling").innerText()).replace(/\s+/g, " ");
  assert.match(await text("over@example.com"), /Fail over at 20%.*At its ceiling: skipped until 5 hours resets in 1h 5\dm/, "equal is at the ceiling");
  const under = await text("under@example.com");
  assert.match(under, /Under it: 5 hours 19%, 7 days 10%\./, "both enforced windows, no model-specific one");
  assert.doesNotMatch(under, /Opus/, "a 99% 7d Opus window is shown in the meters but does not trip the ceiling");
  assert.match(await text("unknown@example.com"), /Usage unknown: tried only after accounts known to be under their ceiling\./);
  assert.doesNotMatch(await text("unknown@example.com"), /(^|[^0-9])0%/, "unknown is never shown as zero");
  assert.doesNotMatch(await text("loading@example.com"), /Under it|unknown|ceiling:/, "no verdict before usage loads");
  assert.match(await text("off@example.com"), /Fail over at off.*Off: used until Anthropic refuses it\./);
  assert(await card(page, "off@example.com").locator("[data-ceiling]").isDisabled(), "slider is inert while off");
  assert.match(await text("under@example.com"), /proxy routes on its own last reply/, "the card says it can disagree with routing");

  let release;
  holdSave = new Promise((resolve) => { release = resolve; });
  await card(page, "over@example.com").locator("[data-ceiling]").fill("30");
  await card(page, "under@example.com").locator("[data-ceiling]").fill("40");
  release();
  holdSave = null;
  for (const end = Date.now() + 5000; posts.length < 2 && Date.now() < end;) await new Promise((r) => setTimeout(r, 50));
  await card(page, "under@example.com").locator(".ceiling", { hasText: "Fail over at 40%" }).waitFor();
  assert.deepEqual(posts.map((p) => p.failover_at), [{ "over@example.com": 30 }, { "under@example.com": 40 }],
    "a change made while another save is in flight is sent after it, not dropped");
  assert.equal(ceilings["under@example.com"], 40);
  await page.unrouteAll();

  const dir = mkdtempSync(join(tmpdir(), "ui-ceiling-")), file = join(dir, "accounts.json");
  writeFileSync(file, JSON.stringify({ main: "a@example.com", accounts: { "a@example.com": "1", "b@example.com": "2" },
    api_key_accounts: { console: { key: "fake-not-a-key", preferred: false } }, renamed: { old: "a@example.com" } }));
  server = spawn("python3", ["-I", join(here, "account_ui_serve_fixture.py"), file, "0"], { stdio: ["ignore", "pipe", "inherit"] });
  const lines = createInterface({ input: server.stdout });
  const [ready] = await once(lines, "line", { signal: AbortSignal.timeout(10000) });
  lines.close();
  await page.goto(`http://127.0.0.1:${Number(ready)}/#accounts`);
  const a = page.locator("#accounts .card", { hasText: "Claude slot 1" });
  await a.locator(".ceiling").waitFor();
  assert.equal(await page.locator("#accounts [data-ceiling]").count(), 2, "subscriptions get a slider, the Console key does not");
  assert.match(await a.locator(".ceiling").innerText(), /off/, "an unset account keeps today's behaviour");
  await a.locator(".ceiling .switch span").click();
  await page.waitForFunction(() => !document.querySelector("#accounts [data-ceiling]").disabled);
  assert.deepEqual(JSON.parse(readFileSync(file, "utf8")).failover_at, { "a@example.com": 90 });
  await a.locator("[data-ceiling]").fill("20");
  await page.waitForFunction(() => document.querySelector("#accounts [data-ceiling-label]").textContent === "20%");
  await page.waitForFunction(() => /Fail over at 20%/.test(document.querySelector("#accounts .ceiling").innerText));
  const onDisk = JSON.parse(readFileSync(file, "utf8"));
  assert.deepEqual(onDisk.failover_at, { "a@example.com": 20 });
  assert.deepEqual(onDisk.renamed, { old: "a@example.com" }, "unrelated config untouched");
  await page.reload();
  await a.locator(".ceiling").waitFor();
  assert.equal(await a.locator("[data-ceiling]").inputValue(), "20", "reload shows the saved ceiling");
  assert.match(await a.locator(".ceiling").innerText(), /Usage unknown/, "offline fixture: unknown, not 0%");
  await a.locator(".ceiling .switch span").click();
  await page.waitForFunction(() => document.querySelector("#accounts [data-ceiling]").disabled);
  assert.equal("failover_at" in JSON.parse(readFileSync(file, "utf8")), false, "switching it off removes the key");
  if (process.env.CEILING_SCREENSHOT) await page.screenshot({ path: process.env.CEILING_SCREENSHOT, fullPage: true });
  console.log("ok ceiling status, save, reload, clear");
} finally {
  await browser.close();
  server?.kill();
}
