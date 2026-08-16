// GET /api/paper_wallet — a ₹50,00,000 paper wallet, sized mechanically
// against every signal the ledger has produced since START_DATE.
//
// The simulation itself (simulateWallet) is pure and lives in
// _paper_wallet.js so it can be unit-tested without a database — this file
// is just the fetch + wire-up. Stateless by design: every number here is
// recomputed fresh from all_signals on each request, the same way
// funds.py and badgeOf() derive everything from raw data rather than a
// mutable running total. There is no wallet-state table to drift from
// reality, so it cannot be corrupted by a bad write.
import { db, str, badgeOf, json, fail, optional } from "./_db.js";
import { simulateWallet, START_DATE } from "./_paper_wallet.js";

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");

  try {
    const gradeCol = await optional("grade");
    const sql = `SELECT id, date, symbol, signal_type, entry, status, lifecycle_status,
                        exit_price, pnl_pct, closed_at, ${gradeCol}
                 FROM all_signals
                 WHERE substr(date,1,10) >= ?
                 ORDER BY date ASC, id ASC`;
    const rs = await db().execute({ sql, args: [START_DATE] });
    const rows = rs.rows.map((r) => ({
      ...r,
      date: str(r.date),
      symbol: str(r.symbol),
      closed_at: str(r.closed_at) || null,
      grade: str(r.grade) || null,
    }));

    const result = simulateWallet(rows, badgeOf);
    json(res, 200, { ok: true, generated_at: new Date().toISOString(), ...result }, 60);
  } catch (e) {
    fail(res, 500, `paper_wallet query failed: ${e.message}`);
  }
}
