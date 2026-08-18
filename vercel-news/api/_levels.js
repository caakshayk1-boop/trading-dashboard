// _levels.js — target-ladder arithmetic. Deliberately DB-free.
//
// This does NOT live in _db.js, and that is the whole point. _db.js imports
// @libsql/client, and the CI step that runs these tests (`cd vercel-news &&
// node --test` in newspaper.yml) never runs npm install — so anything a test
// file imports must pull no packages at all. Putting distinctTargets in _db.js
// broke the build with ERR_MODULE_NOT_FOUND on 2026-08-18, which is the same
// reason simulateWallet takes badgeOf and currencyOf as injected arguments
// rather than importing them.
//
// Its own tiny number coercion for the same reason: num() lives in _db.js.

// Targets closer together than this fraction of RISK are not separate exits.
// Matches MIN_TARGET_GAP_ATR in signals/indicators.py, which now stops the
// generator producing them. This is the read side, for rows already written.
export const MIN_TARGET_GAP_R = 0.5;

function n(v) {
  if (v === null || v === undefined) return null;
  const x = typeof v === "number" ? v : Number(v);
  return Number.isFinite(x) ? x : null;
}

// Drop targets that are not far enough from the previous one to BE a target.
//
// TECHM published entry 1592, stop 1568.12, T1 1673.09, T2 1678.17 — five
// rupees apart on 24 rupees of risk, 0.2R. Ten of 157 open signals had it.
// The generator was fixed on 2026-08-18, but rows already written still carry
// the collapsed pair, and rewriting an issued signal's levels would falsify
// what the engine actually said at the time.
//
// So the stored row is left exactly as it is and the DISPLAY drops the
// duplicate. Nothing is invented: the inner target is kept, because it is the
// one anchored to real resistance, and the one that was never distinct comes
// back null. The page renders a null target as a blank — the honest statement
// that there was only ever one target there.
//
// No stop-loss means no risk to measure against, so nothing is dropped.
// Inventing a floor from a missing stop would blank real targets.
export function distinctTargets(entry, sl, t1, t2, t3) {
  const e = n(entry), s = n(sl);
  const risk = e !== null && s !== null ? Math.abs(e - s) : null;
  if (!risk) return [n(t1), n(t2), n(t3)];
  const floor = MIN_TARGET_GAP_R * risk;
  const out = [];
  let last = null;
  for (const raw of [n(t1), n(t2), n(t3)]) {
    if (raw === null) { out.push(null); continue; }
    if (last !== null && Math.abs(raw - last) < floor) { out.push(null); continue; }
    out.push(raw);
    last = raw;
  }
  return out;
}
