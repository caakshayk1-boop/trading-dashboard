#!/usr/bin/env node
/**
 * render_page.cjs — render one JS-heavy page and print its text.
 *
 * Exists because some sources hold their data behind client-side rendering and
 * an HTTP reader sees an empty shell. ipopremium.in is the case that forced it:
 * the correct detail URL returns 200 to Jina with completely empty Markdown,
 * and 403 to a plain request. The subscription-by-category tables — QIB, HNI,
 * Retail, each with its own multiple — exist only after the page's own scripts
 * have run.
 *
 * NODE, not python-playwright. The build already installs Node Playwright and
 * caches Chromium for the smoke test, so this costs one more script and no new
 * dependency; `pip install playwright` would add a second driver and a second
 * browser download for the same capability.
 *
 * Usage:  node render_page.cjs <url> [timeoutMs]
 * Prints the rendered innerText to stdout. Exits non-zero on failure so the
 * caller can fall through to the next provider.
 */
const { chromium } = require('playwright');

const BLOCKED = ['image', 'media', 'font'];   // nothing here needs pixels

(async () => {
  const url = process.argv[2];
  const timeout = parseInt(process.argv[3] || '35000', 10);
  if (!url) { console.error('usage: render_page.cjs <url> [timeoutMs]'); process.exit(2); }

  let browser;
  try {
    browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    const ctx = await browser.newContext({
      userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
                 '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      locale: 'en-IN', viewport: { width: 1440, height: 900 },
    });
    // Images and fonts are pure download time for a text extraction, and this
    // runs inside a build with a 15-minute cap.
    await ctx.route('**/*', r =>
      BLOCKED.includes(r.request().resourceType()) ? r.abort() : r.continue());

    const page = await ctx.newPage();
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout });
    // networkidle is the wrong wait for pages that poll: it can never settle.
    // Wait for a table to appear, then give late XHR a brief grace period.
    await page.waitForSelector('table, .table, [class*="subscription"]', { timeout: 12000 })
              .catch(() => {});
    await page.waitForTimeout(1200);
    const text = await page.evaluate(() => document.body ? document.body.innerText : '');
    if (!text || text.trim().length < 200) {
      console.error('render_page: page produced no usable text');
      process.exit(3);
    }
    process.stdout.write(text);
  } catch (e) {
    console.error('render_page: ' + (e && e.message ? e.message : e));
    process.exit(1);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
})();
