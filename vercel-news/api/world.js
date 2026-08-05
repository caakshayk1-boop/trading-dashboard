// GET /api/world — the last 24 hours, placed on a map.
//
// Wires only. Headlines are pulled from public RSS, deduplicated, filtered to
// a rolling 24-hour window, tagged to a country and given a tone:
//
//   red    — conflict, disaster, collapse, sanctions, a market breaking
//   green  — a deal signed, a record set, a breakthrough, a ceasefire
//   blue   — everything else, which is most of it
//
// The tagging is keyword matching over country names, demonyms and capitals.
// That is a heuristic and it is wrong sometimes: "Turkey" in a Thanksgiving
// story, "Georgia" the US state. Ambiguous names carry a `strict` flag and
// only match with a country-ish word nearby. It is honest to call this
// approximate, and it needs no API key, which is why the map exists at all.
//
// Query: hours= (default 24, max 72), limit= (default 14, max 40)
import { json, fail, str } from "./_db.js";

const FEEDS = [
  ["Reuters World", "https://news.google.com/rss/search?q=when:1d+world+news&hl=en-US&gl=US&ceid=US:en"],
  ["BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"],
  ["BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml"],
  ["Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"],
  ["CNBC", "https://www.cnbc.com/id/100727362/device/rss/rss.html"],
  ["Google Markets", "https://news.google.com/rss/search?q=when:1d+markets+OR+economy+OR+central+bank&hl=en-US&gl=US&ceid=US:en"],
  ["Google India", "https://news.google.com/rss/search?q=when:1d+India+economy+OR+RBI+OR+Nifty&hl=en-IN&gl=IN&ceid=IN:en"],
];

// Negative first: a headline containing both "deal" and "killed" is not good
// news. Order of evaluation is the tie-break, deliberately.
const RED = /\b(war|invasion|attack|attacks|strike[sd]?|missile|killed|dead|deaths|casualt|bomb|shooting|terror|coup|unrest|riot|protest|crackdown|sanction|tariff|ban|crash|crashes|plunge[sd]?|slump|collapse|default|bankrupt|recession|layoff|job cuts|shut down|earthquake|quake|flood|wildfire|hurricane|cyclone|typhoon|drought|famine|outbreak|epidemic|pandemic|evacuat|hostage|arrest|fraud|scandal|probe|lawsuit|downgrade|crisis|emergency|shortage|blackout)\b/i;
const GREEN = /\b(deal|agreement|accord|treaty|ceasefire|truce|peace|breakthrough|record high|all-time high|surge[sd]?|rally|rallies|soar|jump[sd]?|beat expectations|approve[sd]?|greenlight|signs?|signed|partnership|invest(?:ment|s|ed)?|funding|expansion|upgrade[sd]?|recovery|rebound|milestone|launch(?:es|ed)?|discovery|cure|vaccine|reopen|boost|wins?|won|award)\b/i;

// name → [lat, lon, aliases…]. Centroids are approximate on purpose: this is a
// dot on a 156-cell-wide grid, not a survey. `strict` names are words that
// mean something else in English and need a nearby country cue.
const PLACES = [
  ["United States", 39, -98, ["u.s.", "u.s", "usa", "america", "american", "washington", "white house", "wall street", "new york", "california", "texas", "federal reserve", "biden", "trump"]],
  ["Canada", 56, -106, ["canadian", "ottawa", "toronto"]],
  ["Mexico", 23, -102, ["mexican", "mexico city"]],
  ["Brazil", -10, -52, ["brazilian", "brasilia", "sao paulo"]],
  ["Argentina", -34, -64, ["argentine", "buenos aires"]],
  ["Chile", -33, -71, ["chilean", "santiago"]],
  ["Colombia", 4, -73, ["colombian", "bogota"]],
  ["Venezuela", 7, -66, ["venezuelan", "caracas"]],
  ["United Kingdom", 54, -2, ["uk", "britain", "british", "england", "london", "scotland", "wales", "bank of england"]],
  ["Ireland", 53, -8, ["irish", "dublin"]],
  ["France", 47, 2, ["french", "paris", "macron"]],
  ["Germany", 51, 10, ["german", "berlin", "frankfurt", "bundesbank"]],
  ["Spain", 40, -4, ["spanish", "madrid", "barcelona"]],
  ["Portugal", 39, -8, ["portuguese", "lisbon"]],
  ["Italy", 43, 12, ["italian", "rome", "milan"]],
  ["Netherlands", 52, 5, ["dutch", "amsterdam", "hague"]],
  ["Belgium", 51, 4, ["belgian", "brussels"]],
  ["Switzerland", 47, 8, ["swiss", "zurich", "geneva", "bern"]],
  ["Austria", 47, 14, ["austrian", "vienna"]],
  ["Sweden", 62, 15, ["swedish", "stockholm"]],
  ["Norway", 61, 9, ["norwegian", "oslo"]],
  ["Denmark", 56, 10, ["danish", "copenhagen"]],
  ["Finland", 64, 26, ["finnish", "helsinki"]],
  ["Poland", 52, 19, ["polish", "warsaw"]],
  ["Ukraine", 49, 32, ["ukrainian", "kyiv", "kiev", "zelensky"]],
  ["Russia", 61, 60, ["russian", "moscow", "kremlin", "putin"]],
  ["Belarus", 53, 28, ["belarusian", "minsk"]],
  ["Greece", 39, 22, ["greek", "athens"]],
  ["Turkey", 39, 35, ["turkish", "ankara", "istanbul", "erdogan"], true],
  ["Israel", 31, 35, ["israeli", "jerusalem", "tel aviv", "netanyahu"]],
  ["Palestine", 31.9, 35.2, ["gaza", "palestinian", "west bank", "hamas"]],
  ["Lebanon", 34, 36, ["lebanese", "beirut", "hezbollah"]],
  ["Syria", 35, 38, ["syrian", "damascus"]],
  ["Iraq", 33, 44, ["iraqi", "baghdad"]],
  ["Iran", 32, 53, ["iranian", "tehran"]],
  ["Saudi Arabia", 24, 45, ["saudi", "riyadh", "aramco"]],
  ["United Arab Emirates", 24, 54, ["uae", "dubai", "abu dhabi", "emirati"]],
  ["Qatar", 25, 51, ["qatari", "doha"]],
  ["Kuwait", 29, 47, ["kuwaiti"]],
  ["Bahrain", 26, 50.5, ["bahraini", "manama"]],
  ["Oman", 21, 57, ["omani", "muscat"]],
  ["Yemen", 15, 48, ["yemeni", "houthi", "sanaa"]],
  ["Egypt", 27, 30, ["egyptian", "cairo", "suez"]],
  ["Libya", 27, 17, ["libyan", "tripoli"]],
  ["Algeria", 28, 3, ["algerian", "algiers"]],
  ["Morocco", 32, -6, ["moroccan", "rabat", "casablanca"]],
  ["Tunisia", 34, 9, ["tunisian", "tunis"]],
  ["Nigeria", 9, 8, ["nigerian", "lagos", "abuja"]],
  ["Ghana", 8, -1, ["ghanaian", "accra"]],
  ["Kenya", 0, 38, ["kenyan", "nairobi"]],
  ["Ethiopia", 9, 40, ["ethiopian", "addis ababa"]],
  ["South Africa", -29, 24, ["johannesburg", "cape town", "pretoria"]],
  ["Sudan", 15, 30, ["sudanese", "khartoum"]],
  ["Congo", -1, 22, ["congolese", "kinshasa"]],
  ["India", 22, 79, ["indian", "delhi", "mumbai", "bengaluru", "sensex", "nifty", "rbi", "modi"]],
  ["Pakistan", 30, 70, ["pakistani", "islamabad", "karachi"]],
  ["Bangladesh", 24, 90, ["bangladeshi", "dhaka"]],
  ["Sri Lanka", 7, 81, ["sri lankan", "colombo"]],
  ["Nepal", 28, 84, ["nepali", "kathmandu"]],
  ["Afghanistan", 33, 66, ["afghan", "kabul", "taliban"]],
  ["China", 35, 105, ["chinese", "beijing", "shanghai", "shenzhen", "xi jinping", "pboc"]],
  ["Hong Kong", 22.3, 114.2, ["hang seng"]],
  ["Taiwan", 24, 121, ["taiwanese", "taipei", "tsmc"]],
  ["Japan", 36, 138, ["japanese", "tokyo", "nikkei", "bank of japan"]],
  ["South Korea", 36, 128, ["korean", "seoul", "samsung", "kospi"]],
  ["North Korea", 40, 127, ["pyongyang", "kim jong"]],
  ["Vietnam", 16, 108, ["vietnamese", "hanoi"]],
  ["Thailand", 15, 101, ["thai", "bangkok"]],
  ["Malaysia", 4, 102, ["malaysian", "kuala lumpur", "ringgit"]],
  ["Singapore", 1.3, 103.8, ["singaporean"]],
  ["Indonesia", -2, 118, ["indonesian", "jakarta"]],
  ["Philippines", 13, 122, ["filipino", "manila"]],
  ["Myanmar", 21, 96, ["burmese", "yangon"]],
  ["Australia", -25, 134, ["australian", "sydney", "canberra", "melbourne"]],
  ["New Zealand", -41, 174, ["wellington", "auckland"]],
];

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");
  const q = req.query || {};
  const hours = Math.min(Math.max(parseInt(q.hours, 10) || 24, 1), 72);
  const limit = Math.min(Math.max(parseInt(q.limit, 10) || 14, 1), 40);

  try {
    const settled = await Promise.all(FEEDS.map(([name, url]) => pull(name, url)));
    const cutoff = Date.now() - hours * 3600e3;

    const seen = new Set();
    const items = [];
    for (const list of settled) {
      for (const it of list) {
        // Google News suffixes " - Publisher"; strip it before deduping or the
        // same story from two aggregators counts twice.
        const key = it.title.toLowerCase().replace(/\s+-\s+[^-]{2,40}$/, "").slice(0, 90);
        if (seen.has(key)) continue;
        seen.add(key);
        if (it.ts && it.ts < cutoff) continue;
        items.push(it);
      }
    }
    items.sort((a, b) => (b.ts || 0) - (a.ts || 0));

    const byCountry = new Map();
    for (const it of items) {
      it.tone = toneOf(`${it.title} ${it.summary}`);
      it.places = placesIn(`${it.title} ${it.summary}`);
      for (const p of it.places) {
        if (!byCountry.has(p)) {
          byCountry.set(p, { name: p, red: 0, green: 0, blue: 0, count: 0, top: null });
        }
        const c = byCountry.get(p);
        c[it.tone]++;
        c.count++;
        if (!c.top) c.top = { title: it.title, link: it.link, source: it.source, tone: it.tone };
      }
    }

    const meta = new Map(PLACES.map(([n, lat, lon]) => [n, [lat, lon]]));
    const countries = [...byCountry.values()].map((c) => {
      const [lat, lon] = meta.get(c.name) || [0, 0];
      // A country is red if red outweighs green, green if the reverse. Ties go
      // to blue: two opposite stories is not a signal.
      const tone = c.red > c.green ? "red" : c.green > c.red ? "green" : "blue";
      return { ...c, lat, lon, tone };
    }).sort((a, b) => b.count - a.count);

    json(res, 200, {
      ok: true,
      generated_at: new Date().toISOString(),
      window_hours: hours,
      sources_ok: settled.filter((l) => l.length).length,
      sources_total: FEEDS.length,
      count: items.length,
      tagging: "keyword match over country names, demonyms and capitals — approximate",
      top: items.slice(0, limit).map((it) => ({
        title: it.title, link: it.link, source: it.source,
        published: it.ts ? new Date(it.ts).toISOString() : null,
        summary: it.summary.slice(0, 180),
        tone: it.tone, places: it.places,
      })),
      countries,
      totals: {
        red: countries.filter((c) => c.tone === "red").length,
        green: countries.filter((c) => c.tone === "green").length,
        blue: countries.filter((c) => c.tone === "blue").length,
      },
    }, 900);
  } catch (e) {
    fail(res, 500, `world fetch failed: ${e.message}`);
  }
}

function toneOf(text) {
  if (RED.test(text)) return "red";
  if (GREEN.test(text)) return "green";
  return "blue";
}

// Whole words only. Naive substring matching tagged "campus", "various" and
// "consensus" as the United States via a bare "us" alias, which put a dot on
// the map for stories that never mentioned the country.
const MATCHERS = PLACES.map(([name, lat, lon, aliases, strict]) => {
  const needles = [name.toLowerCase(), ...(aliases || [])]
    .map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return { name, lat, lon, strict,
           re: new RegExp(`(?:^|[^a-z])(?:${needles.join("|")})(?![a-z])`, "i") };
});

const STRICT_CUE = /\b(president|government|minister|capital|country|border|central bank|lira|ankara|istanbul|economy)\b/i;

function placesIn(text) {
  const hits = [];
  for (const m of MATCHERS) {
    if (!m.re.test(text)) continue;
    // Names that are also ordinary English words need a second cue nearby.
    if (m.strict && !STRICT_CUE.test(text)) continue;
    hits.push(m.name);
  }
  return hits.slice(0, 4);
}

// ── RSS ──────────────────────────────────────────────────────────────────────

async function pull(source, url) {
  try {
    const r = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; DailySignal/1.0)" },
      signal: AbortSignal.timeout(7000),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return parseRss(await r.text(), source).slice(0, 40);
  } catch {
    return [];   // one dead feed must never blank the section
  }
}

function parseRss(xml, source) {
  const out = [];
  const blocks = xml.match(/<item[\s\S]*?<\/item>/gi)
                 || xml.match(/<entry[\s\S]*?<\/entry>/gi) || [];
  for (const b of blocks) {
    const title = clean(pick(b, "title"));
    if (!title) continue;
    let link = clean(pick(b, "link"));
    if (!link) {
      const m = b.match(/<link[^>]*href="([^"]+)"/i);
      link = m ? m[1] : "";
    }
    const dateRaw = pick(b, "pubDate") || pick(b, "published") || pick(b, "updated");
    const ts = dateRaw ? Date.parse(clean(dateRaw)) : NaN;
    out.push({
      source,
      title,
      link,
      summary: clean(pick(b, "description") || pick(b, "summary") || "").replace(/<[^>]+>/g, ""),
      ts: Number.isFinite(ts) ? ts : null,
    });
  }
  return out;
}

function pick(block, tag) {
  const m = block.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, "i"));
  return m ? m[1] : "";
}

function clean(s) {
  return str(s)
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;|&apos;/g, "'").replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}
