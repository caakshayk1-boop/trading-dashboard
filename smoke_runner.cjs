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

    // Scroll spy. Three separate bugs have put the nav highlight on the wrong
    // section — a hardcoded header offset, offsetTop measured against a
    // position:relative <main> instead of the document, and hidden sections
    // reporting top 0 and therefore matching everywhere. None of them are
    // visible in the HTML, only in a scrolled viewport. So: park just below
    // each visible section's top and assert the nav agrees with where we are.
    r.spy = await page.evaluate(async () => {
      const links = [...document.querySelectorAll('.nav a[href^="#"]')];
      const ids = links.map(a => a.getAttribute('href').slice(1))
                       .filter(id => document.getElementById(id)?.getClientRects().length);
      const hh = parseInt(getComputedStyle(document.documentElement)
                   .getPropertyValue('--headh')) || 200;
      const bad = [];
      for (const id of ids) {
        const top = document.getElementById(id).getBoundingClientRect().top + window.scrollY;
        window.scrollTo({ top: top - hh + 20, behavior: 'instant' });
        await new Promise(r => setTimeout(r, 160));
        const on = document.querySelector('.nav a.on');
        const got = on ? on.getAttribute('href').slice(1) : 'none';
        if (got !== id) bad.push(id + '→' + got);
      }
      window.scrollTo({ top: 0, behavior: 'instant' });
      return { checked: ids.length, mismatched: bad };
    });

    r.errors = errors;
    r.want = want;
    out.pages[path] = r;
    await ctx.close();
  }

  await browser.close();
  console.log(JSON.stringify(out));
})().catch(e => { console.error('RUNNER FAILED: ' + e.message); process.exit(2); });
