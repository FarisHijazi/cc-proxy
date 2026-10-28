// Real-DOM check of the dashboard's account list in an isolated headless Chromium, with /api mocked.
// Viewports are tall on purpose: Playwright scrolls mid-drag when the drop target is off-screen, which cancels HTML5 drags.
// Run: node tests/account_ui_drag_test.mjs
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

const ACCOUNTS = [
  { alias: "a@example.com", email: "a@example.com", slot: 1, plan: "max", usage: { five_hour: { utilization: 30, resets_at: null } }, usage_at: Date.now() / 1000 - 120 },
  { alias: "b@example.com", email: "b@example.com", slot: 2, token_only: true, usage: {}, disabled: true },
  { alias: "c@example.com", email: "c@example.com", slot: 3, usage: {} },
];

async function open(browser, size, { failSave = false } = {}) {
  const ctx = await browser.newContext({ viewport: size });
  const page = await ctx.newPage();
  const posts = [];
  let accounts = structuredClone(ACCOUNTS), fallback = {};
  const state = () => ({
    accounts, balance: { strategy: "prefer", prefer: "c@example.com" }, strategies: ["session", "prefer"], fallback,
    default_chains: { claude: ["accounts", "codex", "cursor"] }, providers: { reachable: true, models: { codex: ["gpt-6.1-sol"], cursor: ["cursor-agent"] } },
    proxy: { address: "x:1", up: true },
    db: { path: null, bytes: 0, disk_free: 1 }, traffic_days: null,
  });
  await page.route("http://dash.test/**", (route) => {
    const req = route.request(), url = new URL(req.url());
    const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: html });
    if (url.pathname === "/api/config") {
      const change = req.postDataJSON();
      posts.push(change);
      if (failSave) return json({ error: "disk full" }, 500);
      if (change.order) accounts = change.order.map((alias) => accounts.find((a) => a.alias === alias));
      if (change.fallback) fallback = change.fallback;
      return json(state());
    }
    if (url.pathname === "/api/state") return json(state());
    if (url.pathname === "/api/stats") return json({ total: 0, errors: 0, series: {}, by_account: {}, by_host: {}, by_model: {} });
    if (url.pathname === "/api/sessions") return json({ days: 7, limit: 50, traffic_db: false, sessions: [] });
    return json({ rows: [] });
  });
  await page.goto("http://dash.test/");
  await page.locator("#accounts .card").first().waitFor();
  return { page, posts, close: () => ctx.close() };
}

const names = (page) => page.locator("#accounts .card:not([data-target]) .name").allTextContents();
const listed = (page) => page.locator("#accounts .card").evaluateAll((els) => els.map((e) => e.dataset.target || e.querySelector(".name").textContent));
const dragHandle = (page, from, to) =>
  page.locator("#accounts .card").nth(from).locator(".handle").dragTo(page.locator("#accounts .card").nth(to));

const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM || undefined });
try {
  for (const [label, size] of [["desktop", { width: 1280, height: 2400 }], ["phone", { width: 390, height: 3200 }]]) {
    const { page, posts, close } = await open(browser, size);
    const boxes = await page.locator("#accounts .card").evaluateAll((els) => els.map((e) => e.getBoundingClientRect().toJSON()));
    assert.equal(new Set(boxes.map((b) => Math.round(b.x))).size, 1, `${label}: one column`);
    assert.equal(new Set(boxes.map((b) => Math.round(b.width))).size, 1, `${label}: equal widths`);
    assert.ok(boxes[1].y > boxes[0].y + boxes[0].height - 1, `${label}: stacked vertically`);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${label}: no horizontal scroll`);

    // Preserved details.
    const text = await page.locator("#accounts").innerText();
    assert.match(text, /setup-token/); assert.match(text, /preferred/); assert.match(text, /from its last reply, 2m ago/);
    assert.ok(await page.locator("#accounts .card").nth(1).evaluate((e) => e.classList.contains("off")), "disabled card stays dimmed");
    assert.equal(await page.locator("#accounts .card .handle").first().getAttribute("role"), "img");

    // A mouse drags from anywhere on the card, not only the handle: first card dropped on the last.
    await page.locator("#accounts .card").nth(0).locator(".name").dragTo(page.locator("#accounts .card").nth(2));
    await page.waitForFunction(() => document.querySelector("#accounts .name").textContent === "b@example.com");
    assert.deepEqual(posts.map((p) => p.order), [["b@example.com", "c@example.com", "a@example.com"]], `${label}: drag POST`);
    assert.equal(posts[0].fallback.chains.claude, undefined, `${label}: default chain kept`);
    assert.deepEqual(await names(page), ["b@example.com", "c@example.com", "a@example.com"]);

    // Arrow still works and posts the same shape.
    await page.locator("#accounts [data-down]").first().click();
    await page.waitForFunction(() => document.querySelector("#accounts .name").textContent === "c@example.com");
    assert.deepEqual(posts[1].order, ["c@example.com", "b@example.com", "a@example.com"], `${label}: arrow POST`);

    // Toggle still usable (not swallowed by drag wiring).
    await page.locator("#accounts .card").nth(0).locator(".switch span").click();
    assert.ok(posts[2].disabled.includes("c@example.com"), `${label}: toggle POST`);
    await page.locator('#accounts [data-prefer="a@example.com"]').click();
    assert.deepEqual(posts[3].balance, { strategy: "prefer", prefer: "a@example.com" }, `${label}: make preferred`);
    posts.splice(3, 1);

    // Codex moved between the accounts: one enabled account (c; b is off), then Codex, then the rest.
    assert.deepEqual(await listed(page), ["c@example.com", "b@example.com", "a@example.com", "codex", "cursor"]);
    await dragHandle(page, 3, 1);
    await page.waitForFunction(() => document.querySelectorAll("#accounts .card")[1].dataset.target === "codex");
    assert.deepEqual(posts[3].fallback.chains.claude, ["accounts:1", "codex", "accounts", "cursor"], `${label}: interleaved chain`);
    assert.deepEqual(await listed(page), ["c@example.com", "codex", "b@example.com", "a@example.com", "cursor"]);
    assert.match(await page.locator("#accounts").innerText(), /#2 of 5/);

    // Cursor switched off drops out of the chain and stays listed, dimmed, at the end; on again rejoins last.
    await page.locator('#accounts [data-target="cursor"] .switch span').click();
    await page.waitForFunction(() => document.querySelector('#accounts [data-target="cursor"]').classList.contains("off"));
    assert.deepEqual(posts[4].fallback.chains.claude, ["accounts:1", "codex", "accounts"], `${label}: cursor off`);
    await page.locator('#accounts [data-target="codex"] [data-up]').click();
    await page.waitForFunction(() => document.querySelectorAll("#accounts .card")[0].dataset.target === "codex");
    assert.deepEqual(posts[5].fallback.chains.claude, ["codex", "accounts"], `${label}: codex first`);
    await page.locator('#accounts [data-target="cursor"] .switch span').click();
    await page.waitForFunction(() => !document.querySelector('#accounts [data-target="cursor"]').classList.contains("off"));
    assert.deepEqual(posts[6].fallback.chains.claude, ["codex", "accounts", "cursor"], `${label}: cursor back on, last`);
    await close();
    console.log(`ok ${label}`);
  }

  // Touch drags only from the handle, so a finger on the card body still scrolls the page.
  {
    const { page, posts, close } = await open(browser, { width: 390, height: 3200 });
    const touchDrag = (from, to, selector) => page.evaluate(([from, to, selector]) => {
      const cards = document.querySelectorAll("#accounts .card"), start = cards[from].querySelector(selector).getBoundingClientRect();
      const end = cards[to].getBoundingClientRect(), x = end.x + end.width / 2, y = end.y + end.height / 2;
      const fire = (el, type, cx, cy) => el.dispatchEvent(new PointerEvent(type, { bubbles: true, pointerId: 7, pointerType: "touch", button: 0, clientX: cx, clientY: cy }));
      const el = cards[from].querySelector(selector);
      fire(el, "pointerdown", start.x + 2, start.y + 2); fire(el, "pointermove", x, y); fire(el, "pointerup", x, y);
    }, [from, to, selector]);
    await touchDrag(0, 2, ".name");
    await page.waitForTimeout(200);
    assert.equal(posts.length, 0, "touch on the card body does not drag");
    await touchDrag(0, 2, ".handle");
    await page.waitForFunction(() => document.querySelector("#accounts .name").textContent === "b@example.com");
    assert.deepEqual(posts[0].order, ["b@example.com", "c@example.com", "a@example.com"], "touch drag from the handle");
    await close();
    console.log("ok touch");
  }

  // A failed save must not reorder the list or look like success.
  const { page, posts, close } = await open(browser, { width: 1280, height: 2400 }, { failSave: true });
  const before = await names(page);
  await dragHandle(page, 0, 2);
  await page.locator("#error .error").waitFor();
  assert.equal(posts.length, 1);
  assert.deepEqual(await names(page), before, "failed drag leaves order unchanged");
  assert.match(await page.locator("#error").innerText(), /disk full/);
  await close();
  console.log("ok failed-save");
} finally {
  await browser.close();
}
