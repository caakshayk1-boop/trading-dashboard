const { chromium } = require('playwright');

(async () => {
  const base = process.argv[2];
  const pages = JSON.parse(process.argv[3]);
  const browser = await chromium.launch();
  const out = { pages: {}, ok: true };

  for (const [path, want] of Object.entries(pages)) {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const errors = [];
    // A thrown exception is the failure mode that started all this.
    page.on('pageerror', e => errors.push('pageerror: ' + e.message.slice(0, 160)));
    page.on('console', m => {
      if (m.type() !== 'error') return;
      const t = m.text();
      // CSP style-attribute reports are expected and documented; everything
      // else is a real error.
      if (/Content Security Policy/.test(t) && /style/i.test(t)) return;
      errors.push('console: ' + t.slice(0, 160));
    });

    await page.goto(base + path, { waitUntil: 'domcontentloaded', timeout: 30000 });
    // The live layer needs a moment; these are the elements it fills.
    await page.waitForTimeout(9000);

    const r = await page.evaluate(() => {
      const cv = document.getElementById('wmCanvas');
      let painted = 0;
      if (cv) {
        const c = cv.getContext('2d');
        if (c) {
          const d = c.getImageData(0, 0, cv.width, cv.height).data;
          for (let i = 3; i < d.length; i += 4) if (d[i] > 0) painted++;
        }
      }
      const secs = [...document.querySelectorAll('main section.sec')].map(s => s.id);
      const nav = [...document.querySelectorAll('.nav a[href^="#"]')].map(a => a.getAttribute('href').slice(1));
      return {
        sections: secs,
        navMatchesDom: JSON.stringify(nav) === JSON.stringify(secs),
        tickerSegments: document.querySelectorAll('#tickRail .tseg').length / 2,
        tickerItems: document.querySelectorAll('#tickRail .ti').length,
        mapPainted: painted,
        hasCsp: !!document.querySelector('meta[http-equiv="Content-Security-Policy"]'),
        title: document.title,
        domNodes: document.querySelectorAll('*').length,
      };
    });

    r.errors = errors;
    r.want = want;
    out.pages[path] = r;
    await ctx.close();
  }

  await browser.close();
  console.log(JSON.stringify(out));
})().catch(e => { console.error('RUNNER FAILED: ' + e.message); process.exit(2); });
