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

async function open(browser, { reachable = true, usage = USAGE, stall = false, quota, hash = "accounts", onConfig, usageBreakdown } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 2400 } });
  const page = await ctx.newPage();
  const state = {
    accounts: [{ alias: "a@example.com", email: "a@example.com", slot: 1, usage: {} }],
    balance: { strategy: "session" }, strategies: ["session"], traffic_days: null,
    fallback: { chains: { gemini: ["gemini", "codex"] } },
    fallback_defaults: { chains: { claude: ["accounts", "codex", "cursor"], codex: ["codex", "accounts", "cursor"] }, tiers: [] },
    providers: { reachable, models: reachable ? { anthropic: ["claude-opus-5"], codex: ["gpt-6.1-sol"], gemini: ["gemini-3-pro"],
                                                  cursor: [], "cursor-cli": [], kimi: [], grok: [] } : {} },
    provider_usage: usage, provider_quota: quota, proxy: { address: "x:1", up: true }, db: { path: usage ? "/x" : null, bytes: 0, disk_free: 1 },
  };
  await page.route("http://dash.test/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    if (url.pathname === "/api/state") return stall ? undefined : json(state);
    if (url.pathname === "/api/config" && onConfig) return json(onConfig(state, route.request().postDataJSON()));
    if (url.pathname === "/api/stats") return json({ total: 0, errors: 0, series: {}, by_account: {}, by_host: {}, by_model: {} });
    if (url.pathname === "/api/sessions") return json({ days: 7, limit: 50, traffic_db: false, sessions: [] });
    if (url.pathname === "/api/usage" && usageBreakdown) return stall ? undefined : json(usageBreakdown);
    return json({ rows: [] });
  });
  await page.goto(`http://dash.test/#${hash}`);
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
  {
    const { page, close } = await open(browser, { hash: "models" });
    const catalog = page.locator("#catalog");
    await catalog.locator("[data-model]").first().waitFor();
    const all = await catalog.locator("[data-model]").evaluateAll((els) => els.map((e) => e.dataset.model));
    assert.deepEqual(all.sort(), ["claude-opus-5", "gemini-3-pro", "gpt-6.1-sol"], "every model of every signed-in backend is listed");
    assert.match(await catalog.innerText(), /Kimi[\s\S]*not signed in/i, "a backend with no models says why");
    assert.match(await page.locator("#catalog-count").innerText(), /3 models/);
    await page.fill("#catalog-filter", "sol");
    assert.deepEqual(await catalog.locator("[data-model]:visible").evaluateAll((els) => els.map((e) => e.dataset.model)), ["gpt-6.1-sol"]);
    await close();
    console.log("ok model catalog");
  }
  {
    const { page, close } = await open(browser, { stall: true, hash: "models" });
    await page.waitForTimeout(300);
    assert.equal(await page.locator("#catalog [data-model]").count(), 0, "no catalog while state is in flight");
    assert.match(await page.locator("#catalog").innerText(), /Loading/);
    await close();
    console.log("ok catalog loading");
  }
  {
    const posted = [];
    const onConfig = (state, change) => {
      posted.push(change);
      if (change.api_keys.create) return { ...state, api_base: "http://proxy.test:9", created_key: "ccp-full-secret",
        api_keys: [{ id: "abc123", name: change.api_keys.create, prefix: "ccp-full", created: "2026-11-04T00:00:00+00:00" }] };
      return { ...state, api_keys: [] };
    };
    const { page, close } = await open(browser, { hash: "keys", onConfig });
    await page.locator("#keys").getByText("No API keys yet").waitFor();
    await page.fill("#key-name", "laptop");
    await page.click("#key-form button");
    await page.locator("#key-created").getByText("ccp-full-secret").first().waitFor();
    assert.match(await page.locator("#key-created").innerText(), /ANTHROPIC_BASE_URL=http:\/\/proxy\.test:9\s+ANTHROPIC_API_KEY=ccp-full-secret/);
    assert.doesNotMatch(await page.locator("#keys").innerText(), /ccp-full-secret/, "the list shows only the prefix");
    await page.click("[data-revoke]");
    await page.locator("#keys").getByText("No API keys yet").waitFor();
    assert.deepEqual(posted, [{ api_keys: { create: "laptop" } }, { api_keys: { revoke: "abc123" } }]);
    await close();
    console.log("ok api keys");
  }
  {
    const bucket = { ...empty, requests: 2, tokens_reported: 1, tokens: { input: 10, cache_read: 20, output: 5 } };
    const usageBreakdown = { traffic_db: true, tokens_recorded: true,
      total: { ...bucket, sessions: 1, hosts: 1 }, by_host: [], by_account: [], by_session: [],
      by_key: [{ ...bucket, api_key: "laptop <script>" }, { ...empty, requests: 1, api_key: "Unknown / pre-key" }] };
    const { page, close } = await open(browser, { hash: "usage", usageBreakdown });
    await page.locator("#usage-keys tbody tr").first().waitFor();
    assert.match(await page.locator("#usage-keys").innerText(), /laptop <script>/);
    assert.match(await page.locator("#usage-keys").innerText(), /Unknown \/ pre-key/);
    assert.equal(await page.locator("#usage-keys script").count(), 0);
    assert.match(await page.locator("#usage-keys tbody tr").last().innerText(), /unknown/i);
    await close();
    const pending = await open(browser, { hash: "usage", usageBreakdown, stall: true });
    assert.match(await pending.page.locator("#usage-keys").innerText(), /Loading/);
    assert.equal(await pending.page.locator("#usage-keys tbody tr").count(), 0);
    await pending.close();
    console.log("ok API-key usage and loading");
  }
} finally {
  await browser.close();
}
