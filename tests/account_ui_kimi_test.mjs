import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(join(process.env.PLAYWRIGHT_DIR || join(homedir(), "Projects/gittok"), "x.js"))("playwright");
const html = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../scripts/account_ui.html"), "utf8");
const tiers = [
  {name: "opus-sol", match: ["opus", "sol", "kimi-k3.1-pro-highspeed", "k3.1-pro-highspeed"],
    models: {anthropic: "claude-opus-5-5", codex: "gpt-6.1-sol", cursor: "cursor:claude-opus-5", kimi: "kimi-k3.1-pro-highspeed"}},
  {name: "fable-astra", match: ["fable", "astra", "kimi-k3.1-pro", "k3.1-pro"],
    models: {anthropic: "claude-fable-5-1", codex: "gpt-6-astra", cursor: "cursor:claude-fable-5", kimi: "kimi-k3.1-pro"}},
  {name: "sonnet-luna", match: ["sonnet", "haiku", "luna", "kimi-for-coding", "kimi-for-coding-highspeed", "kimi-k3", "k3", "kimi-k3-256k", "k3-256k"],
    models: {anthropic: "claude-sonnet-5-5", codex: "gpt-6-luna", cursor: "cursor:claude-sonnet-5", kimi: "kimi-k3"}},
];
const kimiIds = tiers.flatMap(t => t.match.filter(k => /^(kimi-|k[23])/.test(k)));
const models = Object.fromEntries(["anthropic", "codex", "cursor", "kimi"].map(p => [p, p === "kimi" ? kimiIds : tiers.map(t => t.models[p])]));
const labels = {anthropic: "Claude", codex: "Codex", cursor: "Cursor API", kimi: "Kimi"};
const catalog = Object.fromEntries(Object.entries(models).map(([p, ids]) => [p, {label: labels[p], family: p === "anthropic" ? "claude" : p, models: ids}]));
let release;
const gate = new Promise(resolve => { release = resolve; });
let snapshot = {
  accounts: [], balance: {strategy: "session"}, strategies: ["session"], fallback: {},
  fallback_defaults: {chains: {claude: ["accounts", "codex"], codex: ["codex", "accounts"], kimi: ["kimi", "accounts", "codex"]}, tiers},
  providers: {reachable: true, models, catalog}, provider_usage: null,
  provider_quota: {kimi: {state: "available", source: "Moonshot balance API", at: 123,
    currency: "credits", balances: {available: 0, voucher: 0, cash: 0}, windows: [], note: "Monetary credit, not a token allowance"}},
  proxy: {address: "x:1", up: true}, db: {path: null, bytes: 0, disk_free: 1},
};
const posts = [];
const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM || undefined});
try {
  const page = await browser.newPage({viewport: {width: 1280, height: 1800}});
  await page.route("http://dash.test/**", async route => {
    const url = new URL(route.request().url());
    const json = body => route.fulfill({contentType: "application/json", body: JSON.stringify(body)});
    if (url.pathname === "/") return route.fulfill({contentType: "text/html", body: html});
    if (url.pathname === "/api/state") { await gate; return json(snapshot); }
    if (url.pathname === "/api/config") {
      const change = route.request().postDataJSON(); posts.push(change); snapshot = {...snapshot, ...change}; return json(snapshot);
    }
    if (url.pathname === "/api/sessions") return json({days: 7, limit: 50, traffic_db: true, sessions: [...kimiIds, ...tiers.map(t => t.models.anthropic)].map((model, i) => ({
      session_id: `session-${i}`, last_seen: Date.now() / 1000 - 10, requests: 1, requested_model: model, status: 200,
    }))});
    if (url.pathname === "/api/stats") return json({total: 0, errors: 0, series: {}, by_account: {}, by_host: {}, by_model: {}});
    return json({rows: []});
  });
  await page.goto("http://dash.test/#accounts");
  assert.equal(await page.locator('[data-provider="kimi"]').count(), 0, "no balance or empty answer before first state settles");
  release();
  const card = page.locator('[data-provider="kimi"]').first();
  await card.waitFor();
  assert.match(await card.innerText(), /Available credit 0 credits/);
  assert.match(await card.innerText(), /Voucher credit 0 credits/);
  assert.doesNotMatch(await card.innerText(), /%|Tokens.*0/);
  assert.equal(await card.locator(".meter").count(), 0, "credit is not utilization");
  if (process.env.KIMI_SCREENSHOT) await page.screenshot({path: process.env.KIMI_SCREENSHOT, fullPage: true});
  await page.evaluate(() => { state.provider_quota.kimi = {state: "unavailable", note: "No verified Coding quota API"}; renderProviderCards(); });
  assert.match(await card.innerText(), /Quota: unknown.*No verified Coding quota API/s);
  assert.doesNotMatch(await card.innerText(), /Available credit/);
  await page.evaluate(() => { state.provider_quota.kimi = {state: "error", error: "Kimi balance HTTP 401"}; renderProviderCards(); });
  assert.match(await card.innerText(), /Quota: unknown.*HTTP 401/s);

  await page.goto("http://dash.test/#routing");
  const chain = page.locator('[data-family="kimi"]');
  await chain.waitFor();
  assert.match(await chain.innerText(), /as requested.*Claude accounts.*Codex/s);
  assert.equal(await page.locator('[data-family="claude"] [data-add] option[value="kimi"]').count(), 1);
  await page.locator('[data-family="claude"] [data-add]').selectOption("kimi");
  await page.waitForFunction(() => state.fallback.chains.claude?.includes("kimi"));
  assert.deepEqual(posts.at(-1).fallback.chains.claude, ["accounts", "codex", "kimi"]);
  assert.match(await page.locator('[data-family="claude"]').innerText(), /Kimi · same tier/);
  assert.equal(await page.locator('[data-family="claude"] .missing').count(), 0, "Kimi shorthand resolves availability by provider");

  await page.goto("http://dash.test/#models");
  assert.equal(await page.locator('#models [data-m="kimi"]').count(), 3, "all rows have Kimi destinations");
  await page.locator('#models [data-m="kimi"]').first().fill("kimi-k3.1-pro");
  await page.locator('#models [data-save]').click();
  await page.waitForFunction(() => state.fallback.tiers?.length === 3);
  assert.equal(posts.at(-1).fallback.tiers[0].models.kimi, "kimi-k3.1-pro");
  assert.deepEqual(posts.at(-1).fallback.tiers[2].match, tiers[2].match, "long exact-source row survives editor save");

  await page.evaluate(t => { state.fallback.tiers = t; }, tiers);
  for (const tier of tiers) {
    for (const source of tier.match.filter(k => /^(kimi-|k[23])/.test(k))) {
      for (const target of ["anthropic", "codex", "cursor"]) {
        const chosen = await page.evaluate(([p, m]) => sameTier(p, m), [target, source + "[1m]"]);
        assert.equal(chosen, tier.models[target], `${source} -> ${target}`);
      }
    }
    for (const source of ["anthropic", "codex", "cursor"]) {
      const chosen = await page.evaluate(m => sameTier("kimi", m), tier.models[source]);
      assert.equal(chosen, tier.models.kimi, `${source} -> Kimi`);
    }
  }
  await page.goto("http://dash.test/#sessions");
  await page.locator('#sessions tr[data-sid="session-0"]').waitFor();
  for (let i = 0; i < kimiIds.length; i++) {
    const row = page.locator(`#sessions tr[data-sid="session-${i}"]`);
    assert.equal(await row.locator("[data-provider]").inputValue(), "kimi");
    await row.locator("[data-provider]").selectOption("codex");
    const tier = tiers.find(t => t.match.includes(kimiIds[i]));
    assert.equal(await row.locator("[data-pick]").inputValue(), tier.models.codex, `${kimiIds[i]} session -> Codex`);
  }
  const claudeRow = page.locator(`#sessions tr[data-sid="session-${kimiIds.length}"]`);
  await claudeRow.locator("[data-provider]").selectOption("kimi");
  assert.equal(await claudeRow.locator("[data-pick]").inputValue(), tiers[0].models.kimi);
  await claudeRow.locator("[data-force]").click();
  await page.waitForFunction(() => Object.values(state.session_models || {}).includes("kimi-k3.1-pro-highspeed"));
  assert.deepEqual(posts.at(-1).session_models, {[`session-${kimiIds.length}`]: tiers[0].models.kimi});
  await page.evaluate(() => { document.activeElement.blur(); state.providers.models.kimi = []; renderSessions(); });
  assert.equal(await claudeRow.locator("[data-provider]").inputValue(), "kimi", "unavailable current provider keeps its identity");
  assert.match(await claudeRow.locator("[data-provider] option:checked").innerText(), /unavailable/);
  assert.equal(await claudeRow.locator("[data-exact]").inputValue(), tiers[0].models.kimi);
  assert.equal(await page.evaluate(() => sameTier("kimi", "unmapped-model")), "", "never invent equivalence from newest model");
  assert.equal(await page.evaluate(() => providerOfModel("k3.1-pro[1m]")), "kimi", "raw aliases use catalogue identity");
  await page.evaluate(() => { state.fallback.tiers = [{name: "only", match: ["opus"], models: {codex: "gpt-custom"}}]; });
  assert.equal(await page.evaluate(() => sameTier("kimi", "claude-opus-5-5")), "", "full-replacement tiers do not resurrect Kimi defaults");
  assert.equal(await page.evaluate(() => sameTier("codex", "kimi-k3.1-pro")), "", "missing reverse mapping stays unavailable");
  console.log("ok Kimi balance/loading/errors, routing controls, equivalence editor and all bidirectional aliases");
} finally {
  await browser.close();
}
