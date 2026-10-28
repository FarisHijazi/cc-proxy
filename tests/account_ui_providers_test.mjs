// Real-DOM check of the dashboard's provider cards in an isolated headless Chromium, with /api mocked.
// Run: node tests/account_ui_providers_test.mjs
// Env: PLAYWRIGHT_DIR (dir containing node_modules/playwright; default ~/Projects/gittok),
//      CHROMIUM (browser binary; default: Playwright's own).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.env.PLAYWRIGHT_DIR || join(homedir(), "Projects/gittok");
const { chromium } = createRequire(join(root, "x.js"))("playwright");
const html = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../scripts/account_ui.html"), "utf8");

const NOW = Date.now() / 1000;
const empty = { requests: 0, errors: 0, last_at: null, last_model: null, last_status: null,
                tokens: { input: 0, cache_read: 0, output: 0 }, tokens_reported: 0 };
const USAGE = {
  source: "traffic database", hours: 24, since: NOW - 86400, at: NOW, tokens_capped: false,
  providers: {
    codex: { ...empty, requests: 407, errors: 9, last_at: NOW - 300, last_model: "gpt-6.1-sol", last_status: 200,
             tokens: { input: 2_494_465, cache_read: 90_654_848, output: 174_326 }, tokens_reported: 398 },
    "cursor-cli": empty, cursor: empty, gemini: empty, kimi: empty, grok: empty,
  },
};

async function open(browser, { reachable = true, usage = USAGE, stall = false, quota } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 2400 } });
  const page = await ctx.newPage();
  const state = {
    accounts: [{ alias: "a@example.com", email: "a@example.com", slot: 1, usage: {} }],
    balance: { strategy: "session" }, strategies: ["session"], traffic_days: null,
    fallback: { chains: { gemini: ["gemini", "codex"] } },
    default_chains: { claude: ["accounts", "codex", "cursor"], codex: ["codex", "accounts", "cursor"] },
    providers: { reachable, models: reachable ? { anthropic: ["claude-opus-5"], codex: ["gpt-6.1-sol"], gemini: ["gemini-3-pro"],
                                                  cursor: [], "cursor-cli": [], kimi: [], grok: [] } : {} },
    provider_usage: usage, provider_quota: quota, proxy: { address: "x:1", up: true }, db: { path: usage ? "/x" : null, bytes: 0, disk_free: 1 },
  };
  await page.route("http://dash.test/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    if (url.pathname === "/api/state") return stall ? undefined : json(state);
    if (url.pathname === "/api/stats") return json({ total: 0, errors: 0, series: {}, by_account: {}, by_host: {}, by_model: {} });
    if (url.pathname === "/api/sessions") return json({ days: 7, limit: 50, traffic_db: false, sessions: [] });
    return json({ rows: [] });
  });
  await page.goto("http://dash.test/");
  return { page, close: () => ctx.close() };
}

const card = (page, name) => page.locator(`[data-provider="${name}"]`);

const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM || undefined });
try {
  {
    const { page, close } = await open(browser);
    await card(page, "codex").waitFor();
    const names = await page.locator("#provider-cards .card").evaluateAll((els) => els.map((e) => e.dataset.provider));
    assert.deepEqual(names, ["cursor-cli", "gemini", "kimi", "grok"], "every backend outside the fallback order gets a card");
    const listed = await page.locator("#accounts .card").evaluateAll((els) => els.map((e) => e.dataset.target || e.querySelector(".name").textContent));
    assert.deepEqual(listed, ["a@example.com", "codex", "cursor"], "Codex and Cursor sit in the fallback order after the Claude accounts");
    const boxes = await page.locator("#provider-cards .card").evaluateAll(els => els.map(el => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, bottom: r.bottom };
    }));
    assert(boxes.every(b => Math.abs(b.x - boxes[0].x) < 1 && Math.abs(b.width - boxes[0].width) < 1), "provider cards are one vertical column");
    assert(boxes.slice(1).every((b, i) => b.y >= boxes[i].bottom), "provider cards stack without overlap");
    assert.equal(await page.locator("#provider-cards .switch, #provider-cards .handle, #provider-cards [data-up]").count(), 0,
      "provider cards carry no account toggle, drag handle or arrows");

    const codex = await card(page, "codex").innerText();
    assert.match(codex, /signed in · 1 model/);
    assert.doesNotMatch(codex, /for Claude models/, "its Claude-model position is the list number"); assert.match(codex, /#2 of 3/); assert.match(codex, /#1 of 3 for Codex \/ GPT models/);
    assert.match(codex, /#2 of 2 for Gemini models/);
    assert.match(codex, /407/); assert.match(codex, /2\.5M \/ 90\.7M \/ 174\.3k/);
    assert.match(codex, /last answered 5m ago · gpt-6\.1-sol · HTTP 200/);
    assert.match(codex, /Recorded by the traffic database, last 24 h, as of/);
    assert.match(codex, /Quota: unknown/);
    assert.doesNotMatch(codex, /%/, "no invented quota percentage");

    const kimi = await card(page, "kimi").innerText();
    assert.match(kimi, /unavailable on the proxy host/);
    assert.match(kimi, /In no failover chain/);
    assert.match(kimi, /no model calls answered in the last 24 h/);
    assert.match(await card(page, "gemini").innerText(), /router's estimate/);
    assert.match(await page.locator("#accounts").innerText(), /a@example\.com/, "Claude account cards still render");
    await close();
    console.log("ok recorded usage");
  }
  {
    const { page, close } = await open(browser, { reachable: false, usage: null });
    await card(page, "codex").waitFor();
    const codex = await card(page, "codex").innerText();
    assert.match(codex, /availability unknown/);
    assert.match(codex, /usage unknown/);
    assert.doesNotMatch(codex, /Answered|no model calls/, "no DB is unknown, not zero");
    await close();
    console.log("ok unknown");
  }
  {
    const quota = { "cursor-cli": { source: "cursor DashboardService (cursor-agent login)", at: NOW,
      note: "Pro plan, one budget shared by every client of this Cursor login. Auto 1% · named models 35%", windows: [
      { label: "Included", used: 18.25, limit: 20, unit: "USD", resets_at: new Date((NOW + 3 * 86400) * 1000).toISOString() },
      { label: "API", used: 35.42, limit: 100, unit: "%", resets_at: null },
      { label: "On-demand", used: 2.5, limit: null, unit: "USD", resets_at: null }] },
      codex: { source: "ChatGPT usage API (Codex CLI login)", at: NOW, note: "prolite plan; one budget shared by Codex CLI and the router",
        windows: [{ label: "Weekly", used: 6, limit: 100, unit: "%", resets_at: new Date((NOW + 6 * 86400) * 1000).toISOString() }] } };
    const { page, close } = await open(browser, { quota });
    await card(page, "cursor-cli").waitFor();
    const cli = await card(page, "cursor-cli").innerText();
    assert.match(cli, /Included\s+91%/); assert.match(cli, /18\.25 of 20 USD used/); assert.match(cli, /resets in 2d 23h|resets in 3d/);
    assert.match(cli, /2\.5 USD used, limit not reported/); assert.match(cli, /API\s+35%/); assert.doesNotMatch(cli, /of 100 %/);
    assert.match(cli, /Quota from cursor DashboardService \(cursor-agent login\), as of/); assert.doesNotMatch(cli, /Quota: unknown/);
    const codex = await card(page, "codex").innerText();
    assert.match(codex, /Weekly\s+6%/); assert.match(codex, /Quota from ChatGPT usage API \(Codex CLI login\)/);
    assert.doesNotMatch(codex, /Quota: unknown|of 100 %/);
    assert.match(await card(page, "cursor").innerText(), /Quota: unknown.*own Cursor login/s, "the API backend may use another Cursor login");
    await close();
    console.log("ok vendor quota");
  }
  {
    const { page, close } = await open(browser, { stall: true });
    await page.waitForTimeout(300);
    assert.equal(await page.locator("#provider-cards .card").count(), 0, "nothing rendered while state is in flight");
    assert.match(await page.locator("#provider-cards").innerText(), /Loading/);
    await close();
    console.log("ok loading");
  }
} finally {
  await browser.close();
}
