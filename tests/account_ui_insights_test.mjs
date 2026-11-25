// Real-DOM check of the dashboard's Insights view and Overview "Right now" tiles, /api mocked, light and dark.
// Run: node tests/account_ui_insights_test.mjs
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

const group = (extra) => ({
  requests: 120, per_minute: 2, errors: 3, error_rate: 0.025, fallbacks: 6, fallback_rate: 0.05, statuses: { 200: 117, 429: 3 },
  sessions: 9, active_sessions: 4, machines: 3, active_machines: 2, inflight: 0,
  ttft: { now: 1.25, p50: 1.5, p95: 4.2, samples: 100 }, speed: { now: 61.2, p50: 55, p5: 12, samples: 100 },
  latency: { p50: 8.5, p95: 40 }, throughput_now: 12.5, tokens: { input: 50000, cache_read: 2000000, output: 90000 },
  tokens_reported: 117, cache_hit: 0.975, ...extra,
});
const INSIGHTS = {
  hours: 1, traffic_db: true, timings_recorded: true,
  filters: {accounts: ["a@example.com", "codex"], models: ["claude-opus-5-5", "sol"]},
  health: {current_seconds: 300, baseline_seconds: 3300, models: ["healthy", "degraded", "slow", "unknown"].map((state) => ({
    model: state, ...Object.fromEntries(["speed", "ttft"].map((k) => [k, {state, ratio: state === "unknown" ? null : 0.6,
      current: 42, baseline: 70, samples: 3, baseline_samples: 10}]))
  }))},
  history: {bucket_seconds: 120, points: [20, 60, null, 35].map((speed, i) => ({
    at: Date.now() / 1000 - (4-i) * 120, speed, ttft: speed === null ? null : 1.2,
    speed_samples: speed === null ? 0 : 3, ttft_samples: speed === null ? 0 : 3
  }))},
  live: { at: Date.now() / 1000, connections: 7, client_ips: 3, inflight: 2, dropped: 0 },
  total: group({ inflight: 2 }),
  by_account: [group({ account: "a@example.com", inflight: 2, requests: 100 }),
               group({ account: "codex", requests: 20, ttft: { now: null, p50: null, p95: null, samples: 0 }, tokens: null, cache_hit: null })],
  by_model: [group({ model: "claude-opus-5-5", inflight: 1, requests: 70 }), group({ model: "sol", requests: 50, inflight: 1 })],
};

const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM || undefined });
try {
  for (const colorScheme of ["light", "dark"]) {
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 1600 }, colorScheme })).newPage();
    let release;
    const queries = [];
    const gate = new Promise((resolve) => { release = resolve; });
    await page.route("http://dash.test/**", async (route) => {
      const url = new URL(route.request().url());
      const json = (body) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
      if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
      if (url.pathname === "/api/insights") { queries.push(url); await gate; return json({...INSIGHTS, hours: +url.searchParams.get("hours")}); }
      if (url.pathname === "/api/stats") return json({ total: 0, errors: 0, series: {}, by_account: {}, by_host: {}, by_model: {} });
      if (url.pathname === "/api/state") return route.fulfill({ status: 500, body: "{}" });
      return json({ rows: [], sessions: [] });
    });
    await page.goto("http://dash.test/#overview");
    assert.equal(await page.locator("#now").innerText(), "Loading live insights…", "no numbers before the first answer");
    await page.goto("http://dash.test/#insights");
    assert.equal(await page.locator("#insights-total").innerText(), "Loading insights…");
    assert.equal(await page.locator("#insights-accounts").innerText(), "Loading accounts…");
    release();
    await page.locator("#insights-accounts tr[data-name]").first().waitFor();
    const tiles = await page.locator("#insights-total").innerText();
    for (const want of [/Active sessions[\s\S]*4/, /Open connections[\s\S]*7/, /2 model calls in flight · 3 client IPs/,
                        /61\s*tok\/s/, /1\.25 s/, /p95 4\.20 s/, /2\.0\s*\/min/, /2\.5% errors/, /5\.0% fallback/, /98% cache hit/])
      assert.match(tiles, want);
    assert.equal(await page.locator("#now").textContent(), await page.locator("#insights-total").textContent(), "Overview shows the same tiles");
    const codex = await page.locator('#insights-accounts tr[data-name="codex"]').innerText();
    assert.match(codex, /unknown/, "a group with no samples says unknown, never 0");
    assert.deepEqual(await page.locator("#insights-models tr[data-name]").evaluateAll((rows) => rows.map((r) => r.dataset.name)),
                     ["claude-opus-5-5", "sol"]);
    await page.locator('#insights-models th[data-sort="requests"]').click();
    assert.deepEqual(await page.locator("#insights-models tr[data-name]").evaluateAll((rows) => rows.map((r) => r.dataset.name)),
                     ["claude-opus-5-5", "sol"], "most calls first");
    await page.locator('#insights-models th[data-sort="requests"]').click();
    assert.deepEqual(await page.locator("#insights-models tr[data-name]").evaluateAll((rows) => rows.map((r) => r.dataset.name)),
                     ["sol", "claude-opus-5-5"], "a second click reverses");
    assert.match(await page.locator("#insights-status").innerText(), /429[\s\S]*3/);
    for (const state of ["healthy", "degraded", "slow", "unknown"])
      assert.equal(await page.locator(`#insights-health .health-state.${state}`).count(), 2);
    assert.match(await page.locator("#insights-health").innerText(), /10 earlier \/ 3 recent samples/);
    assert.equal(await page.locator("#insights-history svg").count(), 2);
    assert.equal(await page.locator("#insights-history .mark").count(), 6, "missing buckets have no zero mark");
    assert.equal(await page.locator("#insights-history .series").first().getAttribute("d").then((d) => (d.match(/M/g) || []).length), 2, "gaps break the line");
    await page.locator("#insights-history .hit").first().focus();
    assert.match(await page.locator(".history-tooltip").first().innerText(), /20 tok\/s, 3 samples/);
    await page.locator("#insights-history summary").first().click();
    assert.match(await page.locator("#insights-history table").first().innerText(), /unknown/);
    for (const [minutes, hours] of [[1, 1/60], [5, 5/60], [15, .25]]) {
      await page.getByRole("button", {name: `${minutes}m`, exact: true}).click();
      await page.waitForFunction(() => document.querySelector("#insights-loading").textContent === "");
      assert.equal(+queries.at(-1).searchParams.get("hours"), hours);
      assert.match(await page.locator("#insights-history").innerText(), new RegExp(`last ${minutes} min`));
    }
    await page.locator("#insights-account").selectOption("codex");
    await page.waitForFunction(() => document.querySelector("#insights-loading").textContent === "");
    assert.equal(queries.at(-1).searchParams.get("account"), "codex");
    await page.locator("#insights-model").selectOption("sol");
    await page.waitForFunction(() => document.querySelector("#insights-loading").textContent === "");
    assert.equal(queries.at(-1).searchParams.get("model"), "sol");
    await page.evaluate(() => renderSpeedHistory({bucket_seconds: 2, points: [{at: 1, speed: null, ttft: null, speed_samples: 0, ttft_samples: 0}]}, 1/60));
    assert.equal(await page.locator("#insights-history svg").count(), 0, "an empty selection never draws zero-valued charts");
    assert.match(await page.locator("#insights-history").innerText(), /Unknown · no timing samples/);
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    assert.equal(bg, colorScheme === "dark" ? "rgb(12, 12, 14)" : "rgb(246, 246, 244)");
    await page.setViewportSize({ width: 390, height: 1600 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, "no sideways page scroll on a phone");
  }
  console.log("insights view ok");
} finally {
  await browser.close();
}
