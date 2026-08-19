// _cluster.js — one event, one card.
//
// Dependency-free on purpose: newspaper.yml runs `node --test` in this
// directory with NO npm install, so anything a test reaches must import zero
// packages. Same reason _badge.js and _levels.js exist.
//
// THE PROBLEM
//
// world.js deduplicated on an exact normalised title. That catches the same
// wire republished verbatim and nothing else. Measured on the live feed
// (2026-08-19), the top 40 carried three separate events told three ways:
//
//   RBI Panel Watches Inflation as One Member Eyes Hike This Year
//   RBI MPC minutes: Policy panel signals rate hike risk as inflation rises
//   India rate panel signals impending hikes, eyes inflation path for timing
//
// Three cards, one meeting. A reader scanning the page counts that as three
// things happening, which is exactly the misreading the section exists to
// prevent.
//
// THE MEASURE
//
// Containment, not Jaccard: |A∩B| / min(|A|,|B|). A four-word headline and a
// twelve-word one describing the same event have low Jaccard similarity
// simply because one is longer — containment asks whether the SHORTER
// headline is essentially contained in the longer, which is the right
// question for a wire and its follow-up.
//
// A floor of 3 shared significant tokens sits underneath it, so two short
// headlines cannot cluster on a single coincidental word.

// Three characters, not four. The most identifying token in a financial
// headline is usually an acronym — RBI, SEC, FED, IMF, GDP, IPO — and a
// four-character floor throws away exactly the word that says which event
// this is. The stop list is correspondingly longer to compensate.
const STOP = new Set([
  "says", "with", "from", "after", "that", "this", "will", "have", "been",
  "more", "than", "over", "into", "amid", "could", "among", "about", "their",
  "there", "which", "would", "when", "what", "were", "also", "such", "some",
  "they", "them", "report", "said", "latest", "news", "update",
  // three-letter fillers, admitted by the shorter floor above
  "the", "and", "for", "its", "new", "may", "can", "top", "has", "was",
  "are", "but", "not", "out", "who", "how", "why", "all", "one", "two",
  "his", "her", "you", "our", "why", "did", "get", "now", "see", "set",
]);

/** Crude singular. "hikes" and "hike" are the same event word, and a plural
 *  mismatch on the one token that names the event is enough to split a
 *  cluster — which is exactly how the three RBI stories stayed apart. Not a
 *  stemmer: "ss" endings are left alone so "press" does not become "pres". */
function singular(w) {
  return w.length > 4 && w.endsWith("s") && !w.endsWith("ss") ? w.slice(0, -1) : w;
}

/** Significant tokens in a headline. Publisher suffix and punctuation gone. */
export function tokens(title) {
  return new Set(
    String(title || "")
      .toLowerCase()
      // Google News appends " - Publisher"; it is not part of the event.
      .replace(/\s+[-–|]\s+[^-–|]{2,40}$/, "")
      .replace(/[^a-z0-9\s]/g, " ")
      .split(/\s+/)
      .filter((w) => w.length >= 3 && !STOP.has(w))
      .map(singular)
  );
}

export const MIN_SHARED = 3;
// Tuned against the live top-40 on 2026-08-19, not chosen by feel:
//
//   0.50  merges 2 clusters — leaves 3 RBI MPC stories on separate cards
//   0.40  merges 3 clusters — every member genuinely the same event
//   0.35  merges more, AND pulls "Central Bank of Armenia: exchange rates and
//         prices of precious metals" into the RBI rate-decision cluster
//
// 0.4 is therefore the loosest value with zero false merges on real data.
// Under-merging leaves a duplicate card; over-merging hides a real story
// behind an unrelated one, which is much worse and much harder to notice.
export const MIN_CONTAINMENT = 0.4;

/** Do two headlines describe the same event? */
export function sameEvent(a, b) {
  const ta = a instanceof Set ? a : tokens(a);
  const tb = b instanceof Set ? b : tokens(b);
  if (!ta.size || !tb.size) return false;
  let shared = 0;
  for (const w of ta) if (tb.has(w)) shared++;
  if (shared < MIN_SHARED) return false;
  return shared / Math.min(ta.size, tb.size) >= MIN_CONTAINMENT;
}

/**
 * Collapse a list of items into one entry per event.
 *
 * Order is preserved and the FIRST item of a cluster wins — callers sort by
 * recency before calling, so the primary is the newest telling of the story.
 * Nothing is discarded silently: each survivor carries `also`, the count of
 * other reports, and `also_sources`, so the card can say "+2 more sources"
 * instead of pretending the duplicates never existed.
 */
export function clusterByEvent(items, titleOf = (x) => x.title) {
  const out = [];
  // Every member's tokens, not just the first article's — single linkage.
  //
  // Comparing against the primary alone missed the fourth RBI story: it shared
  // six tokens with the SECOND article in that cluster and only two with the
  // first, so it opened a new card for the same meeting. A cluster's identity
  // is what all of its members say, not what the first one happened to say.
  const sigs = [];
  for (const it of items || []) {
    const t = tokens(titleOf(it));
    let hit = -1;
    for (let i = 0; i < sigs.length && hit === -1; i++) {
      for (const member of sigs[i]) {
        if (sameEvent(t, member)) { hit = i; break; }
      }
    }
    if (hit === -1) {
      sigs.push([t]);
      out.push({ ...it, also: 0, also_sources: [] });
    } else {
      sigs[hit].push(t);
      const p = out[hit];
      p.also += 1;
      const src = it.source;
      // Same publisher filing twice is not a second source.
      if (src && src !== p.source && !p.also_sources.includes(src)) {
        p.also_sources.push(src);
      }
    }
  }
  return out;
}
