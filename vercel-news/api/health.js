// GET /api/health — is the ledger reachable, how fresh is it, and are writes armed.
// First thing to hit when the site looks wrong.
import { db, str, json, columns } from "./_db.js";

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
    // Positions you actually track (the "+ Track" button), NOT signals whose
    // status is OPEN. These are different things and the header used to print
    // both as "open positions", 40px apart, with different numbers.
    out.tracked_positions = Number(tr.rows[0]?.n || 0);
    out.open_positions = out.tracked_positions;   // kept for older clients

    const os_ = await db().execute(
      "SELECT COUNT(*) AS n FROM all_signals WHERE upper(COALESCE(status,''))='OPEN'"
    ).catch(() => ({ rows: [{ n: 0 }] }));
    out.open_setups = Number(os_.rows[0]?.n || 0);

    // Split by engine. The header said "38 open setups" directly above a table
    // showing 3, because the header counted every engine and the table defaults
    // to the gated one. Both were right; only one was labelled.
    const cols0 = await columns();
    if (cols0.has("engine_version")) {
      const ov = await db().execute(
        `SELECT COALESCE(engine_version,'v1') AS v, COUNT(*) AS n
         FROM all_signals WHERE upper(COALESCE(status,''))='OPEN' GROUP BY 1`
      ).catch(() => ({ rows: [] }));
      out.open_by_version = Object.fromEntries(
        ov.rows.map((r) => [str(r.v) || "v1", Number(r.n || 0)])
      );
    }

    const cols = await columns();
    if (cols.has("engine_version")) {
      const vr = await db().execute(
        `SELECT COALESCE(engine_version,'v1') AS v, COUNT(*) AS n
         FROM all_signals GROUP BY 1`
      ).catch(() => ({ rows: [] }));
      out.by_version = Object.fromEntries(
        vr.rows.map((r) => [str(r.v) || "v1", Number(r.n || 0)])
      );
    }

    // 30s at the edge. This was no-store, so every page load paid a cold
    // Turso connect — 2,787ms measured — while the live-ledger bar sat on
    // "Checking live ledger…". Staleness of half a minute is invisible here.
    return json(res, 200, out, 30);
  } catch (e) {
    out.error = e.message;
    return json(res, 503, out);
  }
}
