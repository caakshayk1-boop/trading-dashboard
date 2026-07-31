// GET /api/archive — the date index behind the archive picker.
// One row per trading day: how many signals fired, how they resolved, and the
// day's realised R. This is the "how do I pull up 31 July later" answer — the
// ledger keeps every row, this endpoint just makes the days browsable.
import { db, num, str, badgeOf, json, fail } from "./_db.js";

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");
  const limit = Math.min(Math.max(parseInt((req.query || {}).limit, 10) || 180, 1), 1000);

  try {
    const rs = await db().execute({
      sql: `SELECT substr(date,1,10) AS d, status, lifecycle_status,
                   pnl_pct, r_multiple, entry, sl
            FROM all_signals
            WHERE date IS NOT NULL AND date != ''
            ORDER BY date DESC`,
      args: [],
    });

    const days = new Map();
    for (const r of rs.rows) {
      const d = str(r.d);
      if (!d) continue;
      if (!days.has(d)) days.set(d, { date: d, signals: 0, wins: 0, losses: 0, open: 0, total_r: 0, has_r: false });
      const row = days.get(d);
      row.signals++;
      const badge = badgeOf(r.status, r.lifecycle_status);
      if (badge === "win") row.wins++;
      else if (badge === "loss") row.losses++;
      else if (badge === "open") row.open++;
      const rm = rMultiple(r);
      if (rm !== null) { row.total_r += rm; row.has_r = true; }
    }

    const out = [...days.values()]
      .sort((a, b) => (a.date < b.date ? 1 : -1))
      .slice(0, limit)
      .map((d) => ({
        date: d.date,
        signals: d.signals,
        wins: d.wins,
        losses: d.losses,
        open: d.open,
        total_r: d.has_r ? Math.round(d.total_r * 100) / 100 : null,
      }));

    json(res, 200, { ok: true, generated_at: new Date().toISOString(), days: out }, 300);
  } catch (e) {
    fail(res, 500, `archive query failed: ${e.message}`);
  }
}

function rMultiple(r) {
  const stored = num(r.r_multiple);
  if (stored !== null) return stored;
  const pnl = num(r.pnl_pct), entry = num(r.entry), sl = num(r.sl);
  if (pnl === null || entry === null || sl === null || entry === 0) return null;
  const riskPct = (Math.abs(entry - sl) / entry) * 100;
  return riskPct === 0 ? null : pnl / riskPct;
}
