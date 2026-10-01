/* engines.js — WHICH ENGINES EXIST, AND WHICH ONES THIS SITE STILL PUBLISHES.
 *
 * Loaded by signal.askakshay.com and by gems.askakshay.com, which are separate
 * bundles sharing no runtime — the same arrangement heatcore.js already uses
 * for the heatmap, and for the same reason: two copies of one fact drift, and
 * the drift is invisible because each page stays internally consistent.
 *
 * ── WHAT WENT WRONG WITHOUT IT ──────────────────────────────────────────────
 *
 * On 2026-09-19 this one fact — "which engines publish here" — was written in
 * FOUR places, and three of them were wrong:
 *
 *   signal.js   ENGINE_REGISTRY carried the retirement flags, correctly, and
 *               then defined ENGINES as *every* key including the retired
 *               ones. ENGINES is the ledger's admission gate, so the front
 *               page counted three switched-off engines into its own record.
 *   gems.js     a hand-typed Set of ten keys, two of which — `strict` and
 *               `reclaim` — appear in no feed and never have.
 *   regime.py   its own RETIRED map, four keys stale since August, plus an
 *               EXCLUDE that dropped MULTIBAGGER, an engine the site DOES
 *               publish.
 *   engine_names.py  the documented Python mirror: missing `pivot` entirely,
 *               and still filing `intraday` as ledger-only after the site had
 *               promoted it to GUST.
 *
 * What a reader saw was the front page reporting 65 published / 12 closed /
 * 8.3% won / -0.746R, roughly twenty rows of it from engines the same page
 * said out loud were retired — printed beside a regime panel reporting 11
 * closed / 9.1% from a different population, with nothing to say which was
 * the record. Neither was. The correct population is 45 published, 11 closed,
 * 9.1% won, -0.723R at t=-2.22.
 *
 * Both browser bundles now read THIS file. engine_names.py is the one Python
 * copy, and test_engine_names.py asserts the two are equal in both directions
 * rather than trusting a comment that says they are.
 *
 * ── A RETIREMENT REMOVES AN ENGINE FROM THE RECORD, NOT FROM HISTORY ────────
 *
 * A retired engine keeps its entry here forever. Its closed trades stay in the
 * ledger and must keep rendering as a NAME rather than as a raw database key,
 * so name() and label() never filter. What retirement changes is membership of
 * `live()` — the population every published count is drawn from.
 */
(function (root) {
  'use strict';

  /* The date this site's own record begins. Everything before it belongs to
     news.askakshay.com, under stop rules this site has since said were wrong,
     on a ledger that has been re-graded twice. */
  var LAUNCH = '2026-09-02';

  var REGISTRY = {
    /* PIVOT is the newest and the only one built outward from a framework
       rather than from a pattern: location, then context, then confirmation,
       in that order, with a name that is not AT a level never scored on its
       trend. Its confirmation stage is DAILY-BAR PROXIES — this site has no
       depth feed, so there is no delta, no CVD and no footprint, and the
       engine says so on every signal it files rather than borrowing the
       vocabulary of data it does not have. */
    pivot: {
      name: "PIVOT", role: "Reaction at a level",
      tf: "Daily → weeks", band: null,
      hunts: "Price AT a level the market has already reacted to — its 200-day, the top of a multi-week shelf, or a swing high being retested — with the higher timeframe agreeing and the session confirming it.",
    },
    breakout: {
      name: "BREACH", role: "Breakouts",
      tf: "Daily → weeks", band: null,
      hunts: "Price clearing a level it has been under — 52-week, 20-week and 6-month highs, confirmed on volume.",
    },
    /* ── ONE RECOVERY BAND, NOT TWO ─────────────────────────────────────────
       `magic` (>15% off the high) and `magicmagic` (20-40%) were the same
       screen run twice with a different floor and published under ONE name,
       so the floor showed two TIDAL cards and a name could sit on both.

       The wider band is also the weaker one — 23 closed at -0.193R against 19
       at -0.247R is not a difference either sample can carry — and >15% admits
       every name 20-40% does plus a shallower tail. The deeper fall is the
       whole thesis: more room back to the high. So the narrow band is the
       engine and this one stops publishing.

       RETIRED, NOT DELETED. 23 closed trades carry this key and a deleted
       entry would render them as the raw string `magic` — the exact leak the
       display names exist to prevent. It keeps its name and is filtered out
       of the roster by `retired`. */
    magic: {
      name: "TIDAL", role: "Recovery",
      tf: "Weekly → months", band: ">15% off the high",
      retired: "2026-09-18",
      hunts: "The wider recovery band. Retired — the 20–40% screen is the engine, and this one admitted everything it does plus a shallower tail.",
    },
    magicmagic: {
      name: "TIDAL", role: "Recovery",
      tf: "Weekly → months", band: "20–40% off the high",
      hunts: "The same screen, deeper water — a larger fall, so more room back to the high.",
    },
    /* RETIRED 2026-09-18 ON ITS OWN MEASURE. 16 closed, -0.535R, 25% won,
       t=-2.92 — the only engine on this board whose record is significantly
       NEGATIVE, and the one whose whole claim was that it came from a measured
       edge rather than a pattern. The measure came back and it is against it.
       An engine that loses reliably is a stronger finding than one that does
       nothing, and it is the easiest one to act on. */
    equity_measured: {
      name: "PLUMB", role: "Measured equity",
      tf: "Daily → days", band: null,
      retired: "2026-09-18",
      hunts: "The backtested daily-close engine. Retired on its own record: 16 closed at −0.535R, t=−2.92 — significantly negative.",
    },
    multibagger: {
      name: "ASCENT", role: "Leaders",
      tf: "Weekly → 6–12 months", band: null,
      hunts: "Names already near their highs with institutional volume behind them — CAN SLIM, held for quarters not weeks.",
    },
    momentum_quant: {
      name: "VECTOR", role: "Momentum",
      tf: "Monthly → months", band: null,
      hunts: "Cross-sectional rank over the full NSE screen: six and twelve month returns over one-year sigma, skipping the last month.",
    },
    /* RETIRED 2026-09-18. Not on a bad record — on NO record. It has never
       had a closed trade graded, so there is nothing to defend it with, and
       its weekly-to-months horizon is the one ASCENT already covers. Two
       engines hunting the same ground with one of them unmeasured is the
       duplication this roster has just spent a day removing. */
    ai_longterm: {
      name: "NORTH", role: "Long horizon",
      tf: "Weekly → months", band: null,
      retired: "2026-09-18",
      hunts: "The long-horizon screen, run weekly against the whole board.",
    },
    /* ── THE TWO NEW ONES ───────────────────────────────────────────────────
       Every engine above buys strength that is ALREADY VISIBLE — 52-week
       highs, names near their highs, twelve-month momentum. None of them looks
       for the FIRST move off a base, which is the point where the invalidation
       level is closest and therefore where risk is smallest. These two do,
       from opposite evidence: LEDGE from price going quiet, KEEL from momentum
       refusing to confirm a new low. */
    ledge: {
      name: "LEDGE", role: "Base breakout",
      tf: "Daily → weeks", band: "≥12% off the high",
      hunts: "A Darvas box — price gone quiet in a tight range after a fall — and then a CLOSE out of the top of it on volume.",
    },
    keel: {
      name: "KEEL", role: "Divergence turn",
      tf: "Daily → weeks", band: "≥12% off the high",
      hunts: "A lower low in price against a higher low in RSI, traded only when price reclaims the level it lost.",
    },
    /* ── BROUGHT BACK ───────────────────────────────────────────────────────
       GUST was switched off on 2026-07-30 with the rest of the intraday tier,
       on a measurement of that TIER: -0.005R over 583 trades against +0.171R
       on daily closes. The decision was right about the tier and wrong about
       this engine inside it — on its own seventeen closed trades it reads
       +1.472R at t=3.69 and 70.6% won, the best record on this board by a
       distance, and it was retired for the company it kept.

       IT COMES BACK AT RESEARCH AND NOT ABOVE IT. Seventeen trades clears the
       t-statistic bar and does not come close to the sample one — this site
       requires thirty closed at t >= 2 before an engine is trusted with
       anything, and an engine cannot be exempted from that rule for having
       impressed on a small sample. That is the precise error the rule exists
       to prevent. So it is logged and shown, and never alerted, exactly as
       PIVOT is. */
    intraday: {
      name: "GUST", role: "Intraday momentum",
      tf: "15-minute → the close", band: null,
      hunts: "A 15-minute momentum push on the Nifty 50 universe — VWAP reclaimed, RSI crossing 55, on a volume surge. The only engine here that does not hold overnight.",
    },
  };

  var entries = function () {
    return Object.keys(REGISTRY).map(function (k) { return [k, REGISTRY[k]]; });
  };
  /* THE POPULATION. Every published count on either site comes from this. */
  var live = function () {
    return entries().filter(function (e) { return !e[1].retired; });
  };
  var keys = function () { return live().map(function (e) { return e[0]; }); };
  var names = function () {
    var seen = {}, out = [];
    keys().forEach(function (k) {
      var n = REGISTRY[k].name;
      if (!seen[n]) { seen[n] = 1; out.push(n); }
    });
    return out;
  };

  var get = function (k) { return REGISTRY[String(k || '')] || null; };
  /* Never filtered: a row from a retired engine still renders its name. */
  var name = function (k) { return (get(k) || {}).name || String(k || '—'); };

  /* magic and magicmagic are ONE screen read at two depths and deliberately
     share the name TIDAL. On a card that is right — the band is printed under
     it. In a filter it produced two options reading "TIDAL" with nothing to
     choose between them, so where a name is shared the band is appended. */
  var shared = function (n) {
    return entries().filter(function (e) { return e[1].name === n; }).length > 1;
  };
  var label = function (k) {
    var e = get(k);
    if (!e) return name(k);
    return (shared(e.name) && e.band) ? e.name + ' · ' + e.band : e.name;
  };

  var liveSet = function () {
    var s = {};
    keys().forEach(function (k) { s[k] = 1; });
    return s;
  };

  /* ── IS THIS ROW IN THIS SITE'S RECORD? ────────────────────────────────────
   *
   * Three conditions, and each one has cost a published number:
   *
   *   LIVE ENGINE — see above. The feed also carries `commodity`, `top5_pick`,
   *     `cf_1h`, `ohl` and `sip_bucket`, which are news.askakshay.com's
   *     engines arriving down the same pipe. They are not this site's and are
   *     not counted here.
   *   LONG ONLY — the book is long-only and every engine still FILES shorts;
   *     the ledger keeps them, because deleting them would destroy the
   *     evidence for whether refusing them costs anything. A short was never
   *     sent to anybody, so claiming it in a record titled "every alert this
   *     site has sent" is taking credit for a call nobody received.
   *   SINCE LAUNCH — before it, a different site under different stops.
   *
   * The rupee test is a GUARD, not a filter: applying the engine rule already
   * leaves nothing but Indian names, and if that ever stops being true these
   * pages must not quietly start counting a COMEX gold future.
   */
  var ok = function (r) {
    if (!r) return false;
    var set = liveSet();
    return !!set[String(r.signal_type || '')]
      && String(r.action || 'BUY').toUpperCase() !== 'SELL'
      && String(r.currency || '₹') === '₹';
  };
  var sinceLaunch = function (r) {
    return !!r && String(r.date || '').slice(0, 10) >= LAUNCH;
  };
  var inBook = function (r) { return ok(r) && sinceLaunch(r); };

  root.ENGINE_BOOK = {
    LAUNCH: LAUNCH, REGISTRY: REGISTRY,
    entries: entries, live: live, keys: keys, names: names,
    get: get, name: name, label: label,
    ok: ok, sinceLaunch: sinceLaunch, inBook: inBook,
  };
})(window);

/* ── THE OTHER FACTS THE TWO SITES MUST AGREE ABOUT ──────────────────────────
 *
 * Akshay: "gems not getting refreshed eg. ipo section still shows 5 open —
 * ensure everything is live, correct and the same across the site; gems should
 * follow signal for the data as designed."
 *
 * He was right, and it was not a refresh problem. ipo.json's `open` array
 * carried FIVE entries. signal.askakshay.com ran them through ipoOpenNow()
 * first, which drops a book whose close date has passed, and showed TWO.
 * gems.askakshay.com read `ipo.open` raw and showed five — three of them books
 * that had stopped taking bids, under a heading that said "open".
 *
 * Neither figure came from stale data. They came from one site applying a rule
 * and the other not knowing there was one — the identical shape as the engine
 * roster, where signal filtered retired engines and gems kept its own hand-
 * typed list. A rule that lives in one bundle is a rule the other bundle will
 * contradict.
 *
 * So it moves here, beside the engine roster, and both sites call it. This
 * file is the contract between the two products, not only the engine registry;
 * anything both must agree about belongs in it rather than in whichever one
 * happened to implement it first.
 */
(function (root) {
  'use strict';

  /* Today in IST, as YYYY-MM-DD. en-CA renders that format, and it compares
     correctly as a plain string. An IPO book closes on an Indian calendar day
     regardless of where the reader is — a browser in MYT is eight and a half
     hours ahead and would otherwise close a book early. */
  var istToday = function () {
    try { return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' }); }
    catch (e) { return new Date().toISOString().slice(0, 10); }
  };

  /* Days until the book closes. Falls back to the build's own `days_left` when
     the feed carries no usable close date — keep what the build said rather
     than invent a number. */
  var daysLeft = function (r) {
    if (!r) return null;
    var cd = String(r.close_date || '').slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(cd)) {
      return r.days_left != null ? r.days_left : null;
    }
    return Math.round(
      (Date.parse(cd + 'T00:00:00Z') - Date.parse(istToday() + 'T00:00:00Z')) / 86400000);
  };

  /* Books the calendar still says are taking bids. A negative days_left is a
     closed book and must not appear under a heading that says "open". A null
     is unknown, and unknown is kept rather than dropped — removing a row
     because its date is missing hides it from both counts. */
  var ipoOpenNow = function (list) {
    return (list || []).filter(function (r) {
      var dl = daysLeft(r);
      return dl == null || dl >= 0;
    });
  };

  root.SIGNAL_RULES = {
    istToday: istToday,
    daysLeft: daysLeft,
    ipoOpenNow: ipoOpenNow,
  };
})(window);
