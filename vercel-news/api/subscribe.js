// POST /api/subscribe — the one thing this site could not do.
//
// 16,156 words shipped with zero email inputs and zero mailto links. A reader
// who got to the bottom had nothing to do. This is the capture, and it stores
// to the Turso database that already backs the ledger — no new vendor, no new
// secret, and the list is exportable to any ESP later because it is just a
// table you own.
//
// Deliberately unauthenticated: subscribing is a public action, so EDIT_KEY is
// not involved. That makes abuse the design problem, handled by:
//   · strict RFC-ish validation and a hard length cap
//   · a honeypot field bots fill and humans never see
//   · a minimum time-on-page, because bots post instantly
//   · one row per address (UNIQUE), so replays are idempotent
//   · a per-IP hourly cap enforced in SQL, not memory — lambdas do not share
//     memory, so an in-process limiter would be theatre
//
// Privacy: the raw IP is never stored. It is salted and hashed only so the
// rate limit can work, which is the minimum data that makes the feature safe.
import { db, str, json, fail, readBody } from "./_db.js";
import { createHash } from "node:crypto";

const MAX_EMAIL = 254;                 // RFC 5321
const MAX_PER_IP_PER_HOUR = 5;
const MIN_FORM_SECONDS = 2;

// Intentionally stricter than the RFC: it rejects a handful of legal-but-absurd
// addresses in exchange for rejecting a great deal of junk.
const EMAIL = /^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$/;

const ALLOWED_SOURCES = new Set(["world", "ledger", "footer", "longterm", "unknown"]);

async function ensureTable() {
  await db().execute(`CREATE TABLE IF NOT EXISTS subscribers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL UNIQUE,
    source      TEXT,
    created_at  TEXT NOT NULL,
    ip_hash     TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    user_agent  TEXT
  )`);
  await db().execute(
    "CREATE INDEX IF NOT EXISTS idx_sub_ip_time ON subscribers(ip_hash, created_at)"
  );
}

function clientIp(req) {
  const fwd = str(req.headers["x-forwarded-for"]).split(",")[0].trim();
  return fwd || str(req.headers["x-real-ip"]) || "unknown";
}

// Salted with the Turso token, which is already a deployment secret and never
// leaves the server. Without a salt an IP hash is trivially reversible.
function hashIp(ip) {
  return createHash("sha256")
    .update(`${ip}|${process.env.TURSO_TOKEN || "salt"}`)
    .digest("hex")
    .slice(0, 32);
}

export default async function handler(req, res) {
  if (req.method !== "POST") return fail(res, 405, "POST only");

  if (!process.env.TURSO_URL || !process.env.TURSO_TOKEN) {
    return fail(res, 503, "Subscriptions are not configured on this deployment.");
  }

  try {
    const body = await readBody(req);

    // Honeypot. Named to look worth filling; hidden from humans in CSS.
    if (str(body.company).trim()) {
      // Answer 200 so the bot records success and does not retry with a
      // different shape. Nothing is written.
      return json(res, 200, { ok: true, status: "subscribed" });
    }

    const elapsed = Number(body.elapsed);
    if (Number.isFinite(elapsed) && elapsed < MIN_FORM_SECONDS) {
      return json(res, 200, { ok: true, status: "subscribed" });
    }

    const email = str(body.email).trim().toLowerCase();
    if (!email) return fail(res, 400, "Enter an email address.");
    if (email.length > MAX_EMAIL) return fail(res, 400, "That address is too long.");
    if (!EMAIL.test(email)) return fail(res, 400, "That does not look like an email address.");

    const source = ALLOWED_SOURCES.has(str(body.source)) ? str(body.source) : "unknown";
    const ipHash = hashIp(clientIp(req));

    await ensureTable();

    const recent = await db().execute({
      sql: `SELECT COUNT(*) AS n FROM subscribers
            WHERE ip_hash = ? AND created_at > datetime('now', '-1 hour')`,
      args: [ipHash],
    });
    if (Number(recent.rows[0]?.n || 0) >= MAX_PER_IP_PER_HOUR) {
      return fail(res, 429, "Too many signups from here. Try again in an hour.");
    }

    // Idempotent: re-subscribing is not an error, and it must not leak whether
    // an address is already on the list.
    await db().execute({
      sql: `INSERT INTO subscribers (email, source, created_at, ip_hash, user_agent)
            VALUES (?,?,datetime('now'),?,?)
            ON CONFLICT(email) DO UPDATE SET status='active'`,
      args: [email, source, ipHash, str(req.headers["user-agent"]).slice(0, 180)],
    });

    return json(res, 200, { ok: true, status: "subscribed" });
  } catch (e) {
    return fail(res, 500, `Could not save that: ${e.message}`);
  }
}
