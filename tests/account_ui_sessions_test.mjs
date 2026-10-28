// Real-DOM check of the dashboard's Sessions panel in an isolated headless Chromium, with /api mocked.
// Run: node tests/account_ui_sessions_test.mjs
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

const ONE = "11111111-1111-4111-8111-111111111111";
const TWO = "22222222-2222-4222-8222-222222222222";
const THREE = "33333333-3333-4333-8333-333333333333";
const now = Date.now() / 1000;
const SESSIONS = [
  { session_id: ONE, last_seen: now - 60, requests: 4, host: "box", user: "me", cwd: "/home/me/proj",
    requested_model: "claude-opus-4-7", served_by: "a@example.com/claude-opus-4-7", status: 200,
    remote: { cchq: "https://hq.example.com/#session=abc", claude: "https://claude.ai/code/cshk_x" } },
  { session_id: TWO, last_seen: now - 30, requests: 1, host: "laptop", user: null, cwd: "/w",
    requested_model: "gpt-6.1-sol", served_by: null, status: 429, remote: null },
  { session_id: THREE, last_seen: now - 20, requests: 1, host: "box", user: null, cwd: "/w",
    requested_model: "claude-opus-5-5", served_by: null, status: 200, remote: null },
];

const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM || undefined });
try {
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 2400 } })).newPage();
  const posts = [];
  let forced = { [TWO]: "claude-mythos-1" };
  let releaseSessions;
  const sessionsGate = new Promise((resolve) => { releaseSessions = resolve; });
  const state = () => ({
    accounts: [], balance: { strategy: "session" }, strategies: ["session"], fallback: {}, default_chains: {},
    providers: { reachable: true, models: { anthropic: ["claude-opus-4-7"], codex: ["gpt-6.1-sol-fast", "gpt-6.1-sol", "gpt-6-luna"], "cursor-cli": ["cursor-cli:auto"] } },
    proxy: { address: "x:1", up: true }, db: { path: "/t.db", bytes: 0, disk_free: 1 }, traffic_days: null, session_models: forced,
  });
  await page.route("http://dash.test/**", async (route) => {
    const req = route.request(), url = new URL(req.url());
    const json = (body) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    if (url.pathname === "/api/config") {
      const change = req.postDataJSON();
      posts.push(change);
      forced = { ...forced };
      for (const [sid, model] of Object.entries(change.session_models)) model ? forced[sid] = model : delete forced[sid];
      return json(state());
    }
    if (url.pathname === "/api/state") return json(state());
    if (url.pathname === "/api/sessions") {
      await sessionsGate;
      return json({ days: 7, limit: 50, traffic_db: true, remote_links: { error: null }, sessions: SESSIONS.map((s) => ({ ...s, forced_model: forced[s.session_id] ?? null })) });
    }
    if (url.pathname === "/api/stats") return json({ total: 0, errors: 0, series: {}, by_account: {}, by_host: {}, by_model: {} });
    return json({ rows: [] });
  });
  await page.goto("http://dash.test/");
  await page.locator("#providers .provider").first().waitFor();
  assert.equal(await page.locator("#sessions").innerText(), "Loading sessions…", "nothing but the loading line before sessions arrive");

  releaseSessions();
  const row = (sid) => page.locator(`#sessions tr[data-sid="${sid}"]`);
  await row(ONE).waitFor();
  assert.match(await page.locator("#sessions-note").innerText(), /seen is not proof a session is still running/);
  const one = await row(ONE).innerText();
  assert.match(one, /me@box/); assert.match(one, /~\/proj/); assert.match(one, /claude-opus-4-7/); assert.match(one, /none/);
  assert.match(await row(TWO).innerText(), /HTTP 429/);
  assert.equal(await row(TWO).locator(".forced").innerText(), "claude-mythos-1");
  assert.equal(await row(TWO).locator("[data-pick]").inputValue(), "exact", "an unlisted forced id shows as an exact id");
  assert.equal(await row(TWO).locator("[data-exact]").inputValue(), "claude-mythos-1");
  const providers = await row(ONE).locator("[data-provider] option").allTextContents();
  assert.deepEqual(providers, ["Claude", "Codex", "cursor-agent CLI"]);
  assert.equal(await row(ONE).locator("[data-provider]").inputValue(), "anthropic", "provider follows the session's model");
  assert.equal(await row(ONE).locator("[data-pick]").inputValue(), "claude-opus-4-7");
  assert.equal(await row(TWO).locator("[data-provider]").inputValue(), "anthropic", "a forced Claude id shows its provider");
  assert.equal(await row(THREE).locator("[data-pick]").inputValue(), "exact", "an unlisted model the session asks for stays selected");
  assert.equal(await row(THREE).locator("[data-exact]").inputValue(), "claude-opus-5-5");

  // Remote Control links: cchq and the claude.ai mirror, or a plain note when HQ knows no session.
  assert.equal(await row(ONE).locator('a[href="https://hq.example.com/#session=abc"]').innerText(), "cchq");
  assert.equal(await row(ONE).locator('a[href="https://claude.ai/code/cshk_x"]').innerText(), "claude.ai");
  assert.match(await row(TWO).innerText(), /no Remote Control session found/);

  // Switching provider keeps the tier: Opus on Claude becomes Sol on Codex, not its -fast variant.
  await row(ONE).locator("[data-provider]").selectOption("codex");
  assert.equal(await row(ONE).locator("[data-pick]").inputValue(), "gpt-6.1-sol");
  await row(ONE).locator("[data-force]").click();
  await row(ONE).locator(".forced").waitFor();
  assert.equal(await row(ONE).locator(".forced").innerText(), "gpt-6.1-sol");
  assert.deepEqual(posts.at(-1), { session_models: { [ONE]: "gpt-6.1-sol" } });

  await row(ONE).locator("[data-provider]").selectOption("anthropic");
  await row(ONE).locator("[data-pick]").selectOption("exact");
  assert.equal(await row(ONE).locator("[data-exact]").isVisible(), true);
  await row(ONE).locator("[data-exact]").fill("claude-mythos-2[1m]");
  await row(ONE).locator("[data-force]").click();
  await page.waitForFunction(() => document.querySelector(`#sessions tr[data-sid] .forced`)?.textContent === "claude-mythos-2[1m]");
  assert.deepEqual(posts.at(-1), { session_models: { [ONE]: "claude-mythos-2[1m]" } });

  await row(TWO).locator("[data-clear]").click();
  await page.waitForFunction((sid) => !document.querySelector(`#sessions tr[data-sid="${sid}"] .forced`), TWO);
  assert.deepEqual(posts.at(-1), { session_models: { [TWO]: null } });
  assert.equal(await row(TWO).locator("[data-clear]").isDisabled(), true);
  assert.equal(await row(ONE).locator(".forced").innerText(), "claude-mythos-2[1m]", "clearing one session leaves the other");
  const fits = () => page.locator("#sessions").evaluate((el) => el.scrollWidth <= el.clientWidth + 1);
  assert.equal(await fits(), true, "Force and Clear fit at desktop width without sideways scrolling");
  await page.setViewportSize({ width: 1024, height: 2400 });
  assert.equal(await fits(), true, "and at laptop width");
  console.log("sessions panel ok");
} finally {
  await browser.close();
}
