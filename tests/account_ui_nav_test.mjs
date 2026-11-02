// Sidebar views and the editable equivalent-model groups, in headless Chromium, with /api mocked.
// Run: node tests/account_ui_nav_test.mjs
// Env: PLAYWRIGHT_DIR (default ~/Projects/gittok), CHROMIUM, SHOTS (dir for ui-desktop.png / ui-mobile.png).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.env.PLAYWRIGHT_DIR || join(homedir(), "Projects/gittok");
const { chromium } = createRequire(join(root, "x.js"))("playwright");
const dir = join(dirname(fileURLToPath(import.meta.url)), "../scripts");
const html = readFileSync(join(dir, "account_ui.html"), "utf8"), css = readFileSync(join(dir, "pico.conditional.min.css"));

const DEFAULTS = { chains: { claude: ["accounts", "codex"], codex: ["codex", "accounts"] }, tiers: [
  { name: "opus-sol", match: ["opus", "sol"], models: { anthropic: "claude-opus-5-5", codex: "gpt-6.1-sol", cursor: "cursor:claude-opus-5" } },
  { name: "sonnet-luna", match: ["sonnet", "haiku", "luna"], models: { anthropic: "claude-sonnet-5-5", codex: "gpt-6-luna" } }] };
const EVIL = `<img src=x onerror="window.pwned=1">`;

async function open(browser, size, { defaults = DEFAULTS, failFirst = false, stall = false } = {}) {
  const ctx = await browser.newContext({ viewport: size });
  const page = await ctx.newPage();
  const posts = [];
  let fallback = {}, failures = failFirst ? 1 : 0;
  const state = () => ({
    accounts: [{ alias: "a@example.com", email: "a@example.com", slot: 1, usage: {} }], balance: { strategy: "session" },
    strategies: ["session"], traffic_days: null, fallback, fallback_defaults: defaults,
    providers: { reachable: true, models: { anthropic: ["claude-opus-5-5"], codex: ["gpt-6.1-sol"], cursor: [], "cursor-cli": [], gemini: [], kimi: [], grok: [] }, fallback_defaults: defaults },
    proxy: { address: "x:1", up: true }, db: { path: null, bytes: 0, disk_free: 1 },
  });
  await page.route("http://dash.test/**", (route) => {
    const req = route.request(), url = new URL(req.url());
    const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    if (url.pathname === "/pico.css") return route.fulfill({ contentType: "text/css", body: css });
    if (url.pathname === "/api/config") {
      const change = req.postDataJSON();
      posts.push(change);
      if (change.fallback && "tiers" in change.fallback) {
        if (change.fallback.tiers.some((t) => t.name === "dup")) return json({ error: "duplicate tier name 'dup'" }, 400);
        fallback = { ...change.fallback }; if (!fallback.tiers.length) delete fallback.tiers;
      } else if (change.fallback) fallback = { ...change.fallback, ...(fallback.tiers ? { tiers: fallback.tiers } : {}) };
      return json(state());
    }
    if (url.pathname === "/api/state") { if (stall) return; if (failures-- > 0) return json({ error: "boom" }, 500); return json(state()); }
    if (url.pathname === "/api/stats") return json({ total: 0, errors: 0, series: {}, by_account: {}, by_host: {}, by_model: {} });
    if (url.pathname === "/api/sessions") return json({ days: 7, limit: 50, traffic_db: false, sessions: [] });
    return json({ rows: [] });
  });
  await page.goto("http://dash.test/#models");
  return { page, posts, close: () => ctx.close() };
}
const noPageScroll = (page) => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1);

const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM || undefined });
try {
  { // desktop: views, keyboard, editing
    const { page, posts, close } = await open(browser, { width: 1280, height: 900 });
    await page.locator("#models article").first().waitFor();
    assert.equal(await page.locator("#models article").count(), 2, "starts from the full effective groups");
    assert.equal(await page.locator('[data-view]:not([hidden])').count(), 1, "exactly one view shows");
    assert.equal(await page.locator('.side a[aria-current="page"]').innerText(), "Models");
    for (const [name, selector] of [["Overview", "#stats"], ["Accounts", "#accounts"], ["Routing", "#chains"], ["Sessions", "#sessions"], ["Requests", "#traffic"], ["Models", "#models"]]) {
      await page.locator(".side a", { hasText: name }).click();
      await page.locator(selector).waitFor({ state: "visible" });
      assert.equal(await page.locator("[data-view]:not([hidden])").count(), 1);
      assert.equal(await page.locator("#view-title").innerText(), name);
    }
    await page.locator(".side a", { hasText: "Routing" }).focus();
    await page.keyboard.press("Enter");
    await page.locator("#chains").waitFor({ state: "visible" });
    assert.match(page.url(), /#routing$/, "keyboard activation navigates");
    await page.goto("http://dash.test/#nonsense"); await page.reload();
    assert.equal(await page.locator("#view-title").innerText(), "Overview", "unknown hash falls back to Overview");
    await page.goto("http://dash.test/#models"); await page.reload();
    await page.locator("#models article").first().waitFor();

    const save = page.locator("#models [data-save]");
    assert.equal(await save.isDisabled(), true, "nothing to save before an edit");
    const first = page.locator("#models article").first();
    await first.locator('[data-f="match"]').fill("opus, sol, mythos");
    await first.locator('[data-m="codex"]').fill("sol[1m]");
    await first.locator('[data-f="name"]').fill(EVIL);
    // The periodic refresh must not wipe unsaved input or take focus from the field being typed in.
    const typing = first.locator('[data-f="match"]');
    await typing.focus();
    await page.locator("#refresh").evaluate((x) => x.click());
    await page.waitForTimeout(300);
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.f), "match", "focus stays in the field");
    assert.equal(await page.locator('#models [data-f="match"]').first().inputValue(), "opus, sol, mythos", "unsaved text survives a refresh");
    await page.locator("#models [data-add]").click();
    await page.evaluate(() => document.activeElement.blur());
    await page.locator("#refresh").evaluate((x) => x.click());
    await page.waitForTimeout(300);
    assert.equal(await page.locator('#models [data-f="match"]').first().inputValue(), "opus, sol, mythos", "and survives a refresh with nothing focused");
    assert.equal(await page.locator("#models article").count(), 3, "the added row survives too");
    const added = page.locator("#models article").nth(2);
    await added.locator('[data-f="name"]').fill("extra");
    await added.locator('[data-f="match"]').fill("kimi");
    await added.locator('[data-m="codex"]').fill("gpt-x");
    await page.locator("#models article").nth(1).locator("[data-rm]").click();
    assert.equal(await page.locator("#models article").count(), 2);
    assert.equal(await page.evaluate(() => window.pwned), undefined, "names are text, never markup");
    await save.click();
    await page.waitForFunction(() => document.querySelector("#models [data-reset]") && !document.querySelector("#models [data-reset]").disabled);
    const saved = posts.at(-1).fallback.tiers;
    assert.deepEqual(saved.map((t) => t.name), [EVIL, "extra"]);
    assert.deepEqual(saved[0].match, ["opus", "sol", "mythos"]);
    assert.deepEqual(saved[0].models, { anthropic: "claude-opus-5-5", codex: "sol[1m]", cursor: "cursor:claude-opus-5" });
    assert.deepEqual(saved[1].models, { codex: "gpt-x" }, "an empty destination is omitted, not saved blank");
    await page.reload(); await page.locator("#models article").first().waitFor();
    assert.equal(await page.locator('#models [data-f="name"]').first().inputValue(), EVIL, "the saved groups survive a reload");
    assert.equal(await page.locator("#models article").count(), 2);

    await page.locator(".side a", { hasText: "Routing" }).click();
    await page.locator('[data-on="auth"]').evaluate((x) => x.click());
    await page.waitForFunction(() => document.querySelectorAll("#triggers [data-on]").length);
    assert.equal(posts.at(-1).fallback.tiers.length, 2, "a trigger edit carries the groups along");
    await page.locator(".side a", { hasText: "Models" }).click();
    assert.equal(await page.locator("#models article").count(), 2, "and they are still there");

    await page.locator("#models [data-reset]").click();
    await page.waitForFunction(() => document.querySelectorAll("#models article").length === 2 && document.querySelector("#models [data-reset]").disabled);
    assert.deepEqual(posts.at(-1).fallback.tiers, [], "reset sends an empty list");
    assert.equal(await page.locator('#models [data-f="name"]').first().inputValue(), "opus-sol");

    await page.locator("#models [data-add]").click();
    await page.locator("#models article").nth(2).locator('[data-f="name"]').fill("dup");
    await page.locator("#models article").nth(2).locator('[data-f="match"]').fill("zzz");
    await page.locator("#models article").nth(2).locator('[data-m="codex"]').fill("gpt-y");
    await page.locator("#models [data-save]").click();
    await page.locator("#error .error").waitFor();
    assert.match(await page.locator("#error").innerText(), /duplicate tier name/);
    assert.equal(await page.locator("#models article").count(), 3, "a refused save keeps the edit");
    if (process.env.SHOTS) {
      await page.reload(); await page.locator("#models article").first().waitFor();
      await page.screenshot({ path: join(process.env.SHOTS, "ui-desktop.png"), fullPage: true });
    }
    await close(); console.log("ok desktop");
  }
  {
    const defaults = {...DEFAULTS, tiers: DEFAULTS.tiers.map(({name, match, models}) => ({match, models: Object.fromEntries(Object.entries(models).reverse()), name}))};
    const {page, posts, close} = await open(browser, {width: 1280, height: 900}, {defaults});
    await page.locator("#models article").first().waitFor();
    const codex = page.locator('#models [data-m="codex"]').first();
    await codex.fill("gpt-temporary");
    await codex.fill(DEFAULTS.tiers[0].models.codex);
    await page.locator("#models [data-save]").click();
    await page.waitForFunction(() => document.querySelector("#models [data-save]").disabled);
    assert.deepEqual(posts.at(-1).fallback.tiers, [], "saving the defaults resets regardless of JSON object key order");
    await close(); console.log("ok semantic default reset");
  }
  { // mobile 375
    const { page, close } = await open(browser, { width: 375, height: 800 });
    await page.locator("#models article").first().waitFor();
    assert.equal(await noPageScroll(page), true, "no horizontal page scroll");
    assert.equal(await page.locator("#nav-list").isVisible(), false, "menu collapsed");
    await page.locator("#menu-toggle").click();
    assert.equal(await page.locator("#menu-toggle").getAttribute("aria-expanded"), "true");
    await page.locator(".side a", { hasText: "Accounts" }).click();
    await page.locator("#accounts").waitFor({ state: "visible" });
    assert.equal(await page.locator("#nav-list").isVisible(), false, "choosing a view closes the menu");
    for (const v of ["overview", "routing", "models", "sessions", "requests"]) {
      await page.goto(`http://dash.test/#${v}`); await page.reload();
      assert.equal(await noPageScroll(page), true, `${v}: no horizontal page scroll`);
    }
    await page.goto("http://dash.test/#models"); await page.reload();
    await page.locator("#models article").first().waitFor();
    if (process.env.SHOTS) await page.screenshot({ path: join(process.env.SHOTS, "ui-mobile.png"), fullPage: true });
    await close(); console.log("ok mobile");
  }
  { // first load failure then retry, and a stalled first load
    const { page, close } = await open(browser, { width: 1280, height: 900 }, { failFirst: true });
    await page.locator("#models [data-retry]").waitFor();
    assert.equal(await page.locator("#models article").count(), 0, "no empty editor presented as truth");
    await page.locator("#models [data-retry]").click();
    await page.locator("#models article").first().waitFor();
    await close();
    const s = await open(browser, { width: 1280, height: 900 }, { stall: true });
    await s.page.waitForTimeout(300);
    assert.match(await s.page.locator("#models").innerText(), /Loading/);
    assert.equal(await s.page.locator("#models article").count(), 0);
    await s.close(); console.log("ok failure and loading");
  }
  { // router defaults unknown: nothing guessed
    const { page, close } = await open(browser, { width: 1280, height: 900 }, { defaults: null });
    await page.locator("#models .error").waitFor();
    assert.match(await page.locator("#models").innerText(), /default equivalence groups are unknown/);
    assert.equal(await page.locator("#models article").count(), 0);
    await close(); console.log("ok unknown defaults");
  }
} finally {
  await browser.close();
}
