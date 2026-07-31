// GET /api/news — business wires, refreshed every 3 hours.
//
// The daily shell freezes its news at 06:00 IST. This keeps the World section
// moving through the day without a rebuild. Edge-cached for 3 hours, which is
// the actual refresh interval — the upstream feeds are hit once per window no
// matter how many people load the page.
import { str, json, fail } from "./_db.js";

// Mirrors NEWS_FEEDS in content_cache.py.
const FEEDS = [
  ["BBC Business",   "https://feeds.bbci.co.uk/news/business/rss.xml"],
  ["CNBC",           "https://www.cnbc.com/id/10001147/device/rss/rss.html"],
  ["Yahoo Finance",  "https://finance.yahoo.com/news/rssindex"],
  ["MarketWatch",    "https://feeds.content.dowjones.io/public/rss/mw_topstories"],
  ["Investing.com",  "https://www.investing.com/rss/news.rss"],
  ["Google Finance", "https://news.google.com/rss/search?q=stock+market+finance+business&hl=en&gl=US&ceid=US:en"],
];

const THREE_HOURS = 10800;
const MAX_AGE_HOURS = 24;

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");
  const limit = Math.min(Math.max(parseInt((req.query || {}).limit, 10) || 18, 1), 60);

  try {
    const batches = await Promise.all(FEEDS.map(([source, url]) => pull(source, url)));
    const cutoff = Date.now() - MAX_AGE_HOURS * 3600_000;

    const seen = new Set();
    const items = batches
      .flat()
      .filter((a) => a.title && (!a.ts || a.ts >= cutoff))
      // Wires syndicate the same story; key on a normalised headline so the
      // same event does not fill the section six times.
      .filter((a) => {
        const k = a.title.toLowerCase().replace(/[^a-z0-9 ]/g, "").split(" ").slice(0, 8).join(" ");
        if (seen.has(k)) return false;
        seen.add(k);
        return true;
      })
      .sort((a, b) => (b.ts || 0) - (a.ts || 0))
      .slice(0, limit);

    json(res, 200, {
      ok: true,
      fetched_at: new Date().toISOString(),
      refresh_hours: 3,
      sources_ok: batches.filter((b) => b.length).length,
      sources_total: FEEDS.length,
      count: items.length,
      news: items,
    }, THREE_HOURS);
  } catch (e) {
    fail(res, 500, `news fetch failed: ${e.message}`);
  }
}

async function pull(source, url) {
  try {
    const r = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; DailySignal/1.0)" },
      signal: AbortSignal.timeout(7000),
    });
    if (!r.ok) return [];
    return parseRss(await r.text(), source).slice(0, 6);
  } catch {
    return []; // one dead feed must not take the section down
  }
}

// Deliberately not a full XML parser: RSS items are flat and predictable, and
// pulling a dependency in for six known feeds is not worth the supply chain.
function parseRss(xml, source) {
  const out = [];
  const blocks = xml.split(/<item[\s>]/i).slice(1);
  for (const b of blocks) {
    const title = clean(pick(b, "title"));
    const link = clean(pick(b, "link")) || (b.match(/<link[^>]*href="([^"]+)"/i) || [])[1] || "";
    if (!title) continue;
    const dateStr = pick(b, "pubDate") || pick(b, "dc:date") || pick(b, "published");
    const ts = dateStr ? Date.parse(dateStr.trim()) : NaN;
    out.push({
      source,
      title: title.slice(0, 200),
      link: link.trim(),
      summary: clean(pick(b, "description")).replace(/\s+/g, " ").slice(0, 240),
      published: Number.isFinite(ts) ? new Date(ts).toISOString() : null,
      ts: Number.isFinite(ts) ? ts : null,
    });
  }
  return out;
}

function pick(block, tag) {
  const m = block.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, "i"));
  return m ? m[1] : "";
}

// Feeds mix CDATA, entity-encoded HTML and raw tags in the same field, and
// MarketWatch in particular emits numeric entities for ordinary apostrophes —
// so "We&#x2019;re" has to decode or the headline reads as markup.
const NAMED = {
  lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ",
  ldquo: "“", rdquo: "”", lsquo: "‘", rsquo: "’",
  mdash: "—", ndash: "–", hellip: "…", amp: "&",
};

function clean(s) {
  return str(s)
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/<[^>]+>/g, "")
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => cp(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => cp(parseInt(d, 10)))
    // &amp; is resolved last so "&amp;#39;" cannot turn into a live entity.
    .replace(/&([a-z]+);/gi, (m, n) => NAMED[n.toLowerCase()] ?? m)
    .replace(/\s+/g, " ")
    .trim();
}

function cp(n) {
  return Number.isFinite(n) && n > 0 && n <= 0x10ffff ? String.fromCodePoint(n) : "";
}
