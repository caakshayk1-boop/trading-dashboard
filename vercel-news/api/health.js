// GET /api/health — is the ledger reachable, how fresh is it, and are writes armed.
// First thing to hit when the site looks wrong.
import { db, str, json } from "./_db.js";

export default async function handler(req, res) {
  const out = {
    ok: false,
    turso_configured: Boolean(process.env.TURSO_URL && process.env.TURSO_TOKEN),
    writes_enabled: Boolean(process.env.EDIT_KEY),
    checked_at: new Date().toISOString(),
  };

  if (!out.turso_configured) {
    out.error = "TURSO_URL / TURSO_TOKEN missing from this deployment's environment";
    return json(res, 503, out);
  }

  try {
    const rs = await db().execute(
      "SELECT COUNT(*) AS n, MAX(date) AS latest FROM all_signals"
    );
    const row = rs.rows[0] || {};
    out.ok = true;
    out.signals = Number(row.n || 0);
    out.latest_signal_date = str(row.latest).slice(0, 10) || null;

    const tr = await db().execute(
      "SELECT COUNT(*) AS n FROM stock_tracker WHERE status='active'"
    ).catch(() => ({ rows: [{ n: 0 }] }));
    out.open_positions = Number(tr.rows[0]?.n || 0);

    return json(res, 200, out);
  } catch (e) {
    out.error = e.message;
    return json(res, 503, out);
  }
}
