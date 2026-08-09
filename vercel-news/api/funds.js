// /api/funds — this week's SIP screen.
//
// Read-only. The screen itself is built by funds.py during the 6 AM job (~700
// NAV downloads) and cached in newspaper_funds keyed by ISO week; this route
// only hands back what is already there. Building on demand would hang the
// request behind a third-party API for minutes.
import { db, json, fail } from "./_db.js";

export default async function handler(req, res) {
  if (req.method !== "GET") return fail(res, 405, "GET only");
  try {
    await db().execute(`CREATE TABLE IF NOT EXISTS newspaper_funds (
      week TEXT PRIMARY KEY, payload TEXT
    )`);
    // Newest week wins. Falling back to the previous week rather than 404ing
    // means a Monday build that has not run yet shows last week's screen —
    // stale by days on a number measured in years, which is fine, and better
    // than an empty section.
    const rs = await db().execute(
      "SELECT week, payload FROM newspaper_funds ORDER BY week DESC LIMIT 1"
    );
    if (!rs.rows.length) {
      return json(res, 200, { ok: true, ready: false, categories: [] }, 600);
    }
    const row = rs.rows[0];
    let payload;
    try {
      payload = JSON.parse(String(row.payload));
    } catch {
      return fail(res, 500, "cached fund screen is not valid JSON");
    }
    return json(res, 200, { ...payload, ready: true, week: String(row.week) }, 1800);
  } catch (e) {
    return fail(res, 500, `funds failed: ${e.message}`);
  }
}
