#!/usr/bin/env node
/**
 * test_next_ui.mjs — does /next.html's interactive layer actually work?
 *
 * Why this exists
 * ---------------
 * smoke_test.py asserts that the BROADSHEET renders. Nothing looked at
 * /next.html, and /next.html is now the surface carrying an interactive
 * markets board and a trading brief with a chart, a confidence dial, a
 * scenario switcher, a confluence matrix and a live risk calculator. Every one
 * of those is JavaScript that can break without the page looking broken.
 *
 * Three of the defects this suite was written against were real and shipped:
 *   · the range sliders SNAPPED the published entry off its own value on load,
 *     so the calculator opened showing a risk per share the ledger never
 *     published, and the "you are simulating" banner stayed silent;
 *   · hover opened a tooltip and the click that followed closed it, so on a
 *     desktop a help mark could not be clicked open at all;
 *   · every animated figure froze on its start value in a background tab,
 *     because requestAnimationFrame does not run in one — the confidence score
 *     read 0.
 *
 * Usage:
 *     node test_next_ui.mjs                       # against production
 *     node test_next_ui.mjs http://localhost:4321
 */
import { chromium } from "playwright";

const BASE = (process.argv[2] || "https://news.askakshay.com").replace(/\/$/, "");
const SITE = BASE + "/next.html";
const fails = [];
const ok = (name, cond, detail) => {
  const pass = cond === true;
  if (!pass) fails.push(name + (detail === undefined ? "" : "  -> " + JSON.stringify(detail)));
  console.log(`  ${pass ? "PASS" : "FAIL"}  ${name}` +
              (pass || detail === undefined ? "" : `  -> ${JSON.stringify(detail)}`));
};

// The routes fetch several feeds each; the brief also fetches a price series.
const SETTLE = 7000;

const browser = await chromium.launch();
try {
  console.log(`next-ui: ${SITE}\n`);

  /* ── MARKETS ─────────────────────────────────────────────────────────── */
  console.log("  /markets");
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage();
  const errs = [];
  p.on("pageerror", e => errs.push("pageerror: " + e.message));
  p.on("console", m => { if (m.type() === "error") errs.push("console: " + m.text()); });
  // "Failed to load resource" on its own names nothing. Record the URL and the
  // status alongside it, so a red run points at the endpoint instead of at the
  // browser. Asset 404s from a cold cache are not interesting; API ones are.
  p.on("response", r => {
    if (r.status() >= 400 && new URL(r.url()).pathname.startsWith("/api/"))
      errs.push(`http ${r.status()} ${new URL(r.url()).pathname}${new URL(r.url()).search}`);
  });

  await p.goto(SITE + "#/markets", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(SETTLE);

  const rows = p.locator(".mk");
  const nRows = await rows.count();
  ok("board renders 40+ instruments", nRows > 40, nRows);
  // The enrichment is the whole point of the route: a board with no 52-week
  // context is the three-column ticker this replaced.
  const nRange = await p.locator(".mk .rng-t").count();
  ok("most rows carry a 52-week range", nRange > nRows * 0.8, `${nRange}/${nRows}`);
  const nSpark = await p.locator(".mk .spark").count();
  ok("most rows carry a price series", nSpark > nRows * 0.5, `${nSpark}/${nRows}`);
  ok("rows are real buttons", await rows.first().evaluate(e => e.tagName) === "BUTTON");

  await rows.first().click();
  await p.waitForTimeout(400);
  ok("row drawer opens", await p.locator(".mk-d.open").count() === 1);
  const cells = await p.locator(".mk-d.open .mk-dg > div").count();
  ok("drawer carries 10+ fields", cells >= 10, cells);
  const dtxt = await p.locator(".mk-d.open").first().innerText();
  ok("drawer names the 52-week high", /52-WEEK HIGH/i.test(dtxt));
  ok("drawer has no NaN or undefined", !/NaN|undefined/.test(dtxt));
  await rows.first().click();
  await p.waitForTimeout(350);
  ok("row drawer closes", await p.locator(".mk-d.open").count() === 0);

  // Direction must never be carried by colour alone.
  const signs = await p.locator(".mk-c").evaluateAll(es => es.slice(0, 12).map(e => e.textContent.trim()));
  ok("every change prints its own sign", signs.every(s => /^[+\-−]|—/.test(s)), signs.slice(0, 3));

  const oxM = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  ok("no horizontal overflow at 1440", oxM === 0, oxM);

  /* ── BRIEF ───────────────────────────────────────────────────────────── */
  console.log("\n  /brief");
  await p.goto(SITE + "#/brief", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(SETTLE);

  const body = await p.locator("main").innerText();
  ok("no NaN, undefined or Infinity anywhere", !/NaN|undefined|Infinity/.test(body),
     (body.match(/NaN|undefined|Infinity/g) || []).slice(0, 3));

  // The count-up must land on its value even where rAF never runs.
  const conf = (await p.locator("#dialN").innerText()).trim();
  ok("confidence resolves to a number", /^(\d+|—)$/.test(conf), conf);

  ok("chart draws from real closes", await p.locator("#pxc .price").count() === 1);
  const cap = await p.locator(".b-cap").first().innerText();
  ok("chart says how many closes it drew", /\d+ real daily closes/.test(cap), cap.slice(0, 80));

  /* EVERY REWARD-TO-RISK FIGURE SAYS WHICH TARGET IT MEASURES TO.
   *
   * This was `/R:R TO T1/ && /R:R TO T2/`, which encodes an assumption the
   * ledger does not hold: that every signal has two targets. The API blanks a
   * second target sitting inside 0.5R of the first, and on such a row the
   * brief prints ONE figure labelled "Reward : risk" and says no second target
   * was published — rather than the old behaviour, which put T1's price back
   * under T2's label and printed the same ratio twice under two names.
   *
   * Which row the brief ranks first is a fact about today's data, so the old
   * form was a flake that would read as a regression in the page. The same
   * assertion in the signal repo's test/ui.mjs failed for exactly this on
   * 2026-09-10; this is the same fix.
   *
   * The invariant is the one the original comment names — no BARE ratio. */
  const twoTargets = /R:R TO T2/i.test(body);
  ok("R:R is labelled by target",
     twoTargets
       ? /R:R TO T1/i.test(body)
       : /REWARD\s*:\s*RISK/i.test(body) && /NOT PUBLISHED/i.test(body),
     twoTargets ? "two targets" : "one target");
  // And the two readings are never the same number under two names, which is
  // exactly what the collapsed-target fallback produced.
  if (twoTargets) {
    const rr = [...body.matchAll(/R:R TO T([12])\s*\n?\s*([\d.]+)/gi)].map(m => m[2]);
    ok("the two R:R readings are different numbers",
       rr.length < 2 || rr[0] !== rr[1], rr);
  }

  // No probability may be attached to a scenario: no model publishes one.
  await p.locator('.b-scb button[data-sc="2"]').click();
  await p.waitForTimeout(250);
  ok("bearish scenario selects", await p.locator('.b-scb button[data-sc="2"]').getAttribute("aria-pressed") === "true");
  ok("scenarios quote no probability", !/probability/i.test(await p.locator("#scPane").innerText()));

  await p.locator(".b-mxr").first().click();
  await p.waitForTimeout(300);
  ok("confluence row expands", await p.locator(".b-mxr.open").count() === 1);

  await p.locator("#cvComp").click(); await p.waitForTimeout(300);
  ok("components view opens every row", await p.locator(".b-crow.open").count() === 5);
  await p.locator("#cvScore").click(); await p.waitForTimeout(300);
  ok("score view closes them", await p.locator(".b-crow.open").count() === 0);

  /* THE SLIDER DEFECT. A range input snaps its value to min + n·step, so a
   * rounded step moved the published entry the instant the page loaded. The
   * calculator must open on the ledger's own numbers. */
  ok("simulation banner is silent at published levels", await p.locator("#rkSim").isHidden());
  const published = await p.evaluate(() => {
    const m = [...document.querySelectorAll(".b-m")]
      .map(e => e.innerText.split("\n").map(s => s.trim()));
    const get = k => (m.find(x => x[0].toUpperCase() === k) || [])[1];
    return { entry: get("ENTRY"), stop: get("STOP") };
  });
  const sliderE = await p.locator("#slE").inputValue();
  const entryNum = Number(String(published.entry).replace(/[^\d.]/g, ""));
  ok("entry slider holds the published entry exactly",
     Math.abs(Number(sliderE) - entryNum) < 0.005, { sliderE, published: published.entry });

  const rk0 = await p.locator("#rkOut").innerText();
  await p.locator("#slS").evaluate(e => {
    e.value = String(Number(e.value) * 0.95);
    e.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await p.waitForTimeout(1200);
  ok("moving the stop changes position size", rk0 !== await p.locator("#rkOut").innerText());
  ok("simulation banner appears once a level moves", await p.locator("#rkSim").isVisible());
  await p.locator("#rkReset").click();
  await p.waitForTimeout(1200);
  ok("reset restores the published levels", await p.locator("#rkSim").isHidden());

  // Crosshair
  await p.locator("#b-chart").scrollIntoViewIfNeeded();
  await p.waitForTimeout(700);
  const box = await p.locator("#pxhit").boundingBox();
  await p.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2);
  await p.waitForTimeout(300);
  ok("crosshair read-out appears", await p.locator("#pxt.on").count() === 1);
  const tipTxt = await p.locator("#pxt").innerText();
  ok("read-out carries a date and a price", /\d{4}-\d{2}-\d{2}/.test(tipTxt), tipTxt.slice(0, 50));

  /* THE TOOLTIP DEFECT. Hover opened the card and the click that followed
   * closed it, so it could not be clicked open on a desktop at all. */
  const tb = p.locator(".tipb").first();
  await tb.scrollIntoViewIfNeeded();
  await p.waitForTimeout(600);
  await tb.click();
  await p.waitForTimeout(300);
  ok("a help mark opens on click", await p.locator("#tipcard.on").count() === 1);
  const cb = await p.locator("#tipcard").boundingBox();
  ok("the card stays inside the viewport",
     cb.x >= 0 && cb.x + cb.width <= 1440 && cb.y >= 0, cb);
  await p.keyboard.press("Escape");
  await p.waitForTimeout(200);
  ok("Escape closes it", await p.locator("#tipcard.on").count() === 0);

  /* Section jump must clear all three sticky layers.
   *
   * THE DIGIT IS READ FROM THE PAGE, NOT WRITTEN DOWN HERE. This pressed "4",
   * which was the trade plan's key when it was written. A Business section now
   * sits ahead of it, so every key after it shifted by one and "4" reaches
   * Business — leaving this to measure #b-plan far down the page and report a
   * moved section as a broken sticky stack. The chip prints its own key, so
   * the test asks the page which digit reaches the plan. */
  const planKey = (await p.locator('#qnav a[data-jump="b-plan"] .kb').innerText()).trim();
  ok("the trade plan chip advertises a key", /^\d$/.test(planKey), planKey);
  await p.keyboard.press(planKey);
  await p.waitForTimeout(1000);
  const planTop = await p.locator("#b-plan").evaluate(e => Math.round(e.getBoundingClientRect().top));
  ok("a section jump clears the sticky stack", planTop > 90 && planTop < 240, planTop);


  /* THE BRIEF MUST NOT SWAP THE INSTRUMENT UNDER THE READER.
   * briefSym was consumed on first use, so the 60-second repaint fell back to
   * "highest reward-to-risk" and quietly changed which company was on screen
   * while someone was reading it. */
  await p.goto(SITE + "#/signals", { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(SETTLE);
  const nLinks = await p.locator("a.brief-link").count();
  ok("signal cards offer a Full brief link", nLinks > 0, nLinks);
  if (nLinks > 1) {
    const link = p.locator("a.brief-link").nth(1);   // deliberately not the default pick
    const wanted = await link.evaluate(a => a.dataset.brief);
    await link.click();
    await p.waitForTimeout(SETTLE + 1500);
    const opened = (await p.locator(".b-hero h1").innerText()).split(" ")[0];
    ok("the brief opens the symbol that was clicked", opened === wanted, { opened, wanted });
    await p.evaluate(() => window.dispatchEvent(new HashChangeEvent("hashchange")));
    await p.waitForTimeout(SETTLE + 1500);
    const after = (await p.locator(".b-hero h1").innerText()).split(" ")[0];
    ok("a repaint does not swap the instrument", after === wanted, { after, wanted });
  }

  /* LADDER LABELS MUST NOT OVERLAP — AND THE RULES MUST NOT MOVE.
   * Two levels a rupee apart land two pixels apart on a linear scale, so their
   * labels printed on top of each other. Only the text may be nudged: the rule
   * stays on its true price, which is the whole claim the chart makes. */
  const lvls = await p.locator(".b-lvl").evaluateAll(els => els.map(e => {
    const tag = e.querySelector(".b-lvl-tag").getBoundingClientRect();
    const line = e.querySelector(".b-lvl-line").getBoundingClientRect();
    return { name: e.dataset.lvl, top: tag.top, bottom: tag.bottom, rule: line.top,
             trueTop: parseFloat(e.style.top) };
  }));
  lvls.sort((a, b) => a.top - b.top);
  let clash = null;
  for (let i = 1; i < lvls.length; i++)
    if (lvls[i].top < lvls[i - 1].bottom - 0.5) clash = [lvls[i - 1].name, lvls[i].name];
  ok("no two ladder labels overlap", clash === null, clash);
  // Ordering by rule position must still match ordering by price.
  const byRule = lvls.slice().sort((a, b) => a.rule - b.rule).map(x => x.name);
  const byPrice = lvls.slice().sort((a, b) => a.trueTop - b.trueTop).map(x => x.name);
  ok("the rules still sit in true price order",
     JSON.stringify(byRule) === JSON.stringify(byPrice), { byRule, byPrice });
  // A nudged label must not be pushed onto the caption underneath it.
  const capGap = await p.evaluate(() => {
    const rows = [...document.querySelectorAll(".b-lvl .b-lvl-tag")]
      .map(e => e.getBoundingClientRect().bottom);
    const cap = document.querySelector(".b-chart .b-cap");
    return cap ? Math.round(cap.getBoundingClientRect().top - Math.max(...rows)) : 99;
  });
  ok("the lowest label clears the caption", capGap >= 8, capGap + "px");

  ok("no JS errors on either route", errs.length === 0, errs.slice(0, 3));
  await ctx.close();

  /* ── REDUCED MOTION ──────────────────────────────────────────────────── */
  console.log("\n  prefers-reduced-motion: reduce");
  const rmCtx = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
  const rp = await rmCtx.newPage();
  await rp.goto(SITE + "#/brief", { waitUntil: "domcontentloaded" });
  await rp.waitForTimeout(SETTLE);
  ok("every section is visible", await rp.locator(".b-reveal:not(.in)").count() === 0);
  ok("every chart overlay is visible", await rp.locator(".b-ov:not(.on)").count() === 0);
  ok("the confidence figure is written", /^\d+$/.test((await rp.locator("#dialN").innerText()).trim()));
  // The assertion is that the FILL STEP RAN, not that every score is positive:
  // a component genuinely scoring 0 renders a 0% bar, and treating that as
  // "never filled" is the test inventing a defect. Every bar must carry a
  // width, and at least one must be non-zero.
  const widths = await rp.locator(".b-crow .tr i").evaluateAll(es => es.map(e => e.style.width));
  ok("every score bar was given a width", widths.length > 0 && widths.every(w => !!w), widths);
  ok("at least one score bar is non-zero", widths.some(w => w && w !== "0%"), widths);
  await rmCtx.close();

  /* ── NARROW ──────────────────────────────────────────────────────────── */
  console.log("\n  390 x 844");
  const mCtx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const mp = await mCtx.newPage();
  for (const route of ["#/", "#/markets", "#/brief"]) {
    await mp.goto(SITE + route, { waitUntil: "domcontentloaded" });
    await mp.waitForTimeout(SETTLE);
    const ox = await mp.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    ok(`${route} does not scroll sideways`, ox === 0, ox);
  }
  // Seven tabs in a six-column grid pushed the bar 64px past a 390px phone.
  const tabFit = await mp.evaluate(() => {
    const t = document.querySelector(".tabs");
    return Math.round(t.getBoundingClientRect().width) <= document.documentElement.clientWidth;
  });
  ok("the tab bar fits the viewport", tabFit === true);
  await mCtx.close();
} finally {
  await browser.close();
}

console.log("");
if (fails.length) {
  console.log("FAILED");
  for (const f of fails) console.log("  · " + f);
  process.exit(1);
}
console.log("next-ui: ALL CHECKS PASSED");
