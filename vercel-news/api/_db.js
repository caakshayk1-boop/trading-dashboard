// Shared Turso client + helpers for every /api route.
// TURSO_URL / TURSO_TOKEN are the same secrets the GitHub Actions scanner uses,
// so the site reads the exact ledger the bot writes — no copy, no drift.
import { createClient } from "@libsql/client";
import { timingSafeEqual, createHmac } from "node:crypto";

let _client = null;

export function db() {
  if (_client) return _client;
  const url = process.env.TURSO_URL;
  const authToken = process.env.TURSO_TOKEN;
  if (!url || !authToken) {
    throw new Error("TURSO_URL / TURSO_TOKEN not configured");
  }
  _client = createClient({ url, authToken });
  return _client;
}

// SQLite REAL columns can hold NaN/Infinity (the scanner has written them before).
// JSON.stringify turns those into bare NaN tokens, which breaks JSON.parse in the
// browser. Everything numeric leaving these routes goes through here.
export function num(v) {
  if (v === null || v === undefined) return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function str(v) {
  return v === null || v === undefined ? "" : String(v);
}

// The scanner adds columns via ALTER TABLE inside init_db(), which runs at the
// start of a scan — so this API can be deployed before the column it wants
// exists. Naming a missing column in a SELECT fails the whole query and 500s
// the page, so every optional column is probed first. Cached per lambda
// instance; a cold start re-probes, which is how a new column gets picked up.
// Keyed BY TABLE. This was a single shared variable, so the first table probed
// in a lambda answered for every other one — asking for sip_holdings columns
// after all_signals returned all_signals' column set, silently.
const _colCache = new Map();

export async function columns(table = "all_signals") {
  if (_colCache.has(table)) return _colCache.get(table);
  let cols;
  try {
    const rs = await db().execute(`PRAGMA table_info(${table})`);
    cols = new Set(rs.rows.map((r) => str(r.name || r[1])));
  } catch {
    cols = new Set();
  }
  _colCache.set(table, cols);
  return cols;
}

// Returns `col` when the table has it, else `NULL AS col`, so callers can build
// a SELECT list that is always valid.
export async function optional(col, table = "all_signals") {
  const cols = await columns(table);
  return cols.has(col) ? col : `NULL AS ${col}`;
}

// Moved to _badge.js (zero imports, needed by vercel-news/test/*.test.js —
// see that file's own comment for why). Re-exported here so every existing
// `import { badgeOf } from "./_db.js"` across the API routes keeps working
// unchanged.
export { badgeOf } from "./_badge.js";

// Mirrors _unit() in standalone_scan.py. The ledger holds NSE equities in
// rupees alongside commodities and FX quoted in dollars, and the table rendered
// every one of them with a ₹ — Brent crude at "₹83.59", gold at "₹4,135.80".
// Derived from the symbol because the ledger has no currency column.
const USD_SYMBOLS = new Set([
  "GOLD", "SILVER", "CRUDE", "NATGAS", "NGAS", "COPPER",
  "XAUUSD", "XAGUSD", "WTI", "WTIUSD", "BRENT", "BRNUSD",
]);
// Rates, not money amounts: "1.1517", not "$1.1517".
const FX_PAIRS = new Set([
  "USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD",
  "USDCHF", "USDCAD", "EURJPY", "GBPJPY",
]);

export function currencyOf(symbol) {
  const s = str(symbol).toUpperCase().replace(/\.(NS|BO)$/, "");
  if (USD_SYMBOLS.has(s)) return "$";
  if (FX_PAIRS.has(s)) return "";
  return "₹";   // NSE equities, and the INR pairs where ₹ is correct
}

export function json(res, status, body, cacheSeconds = 0) {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader(
    "Cache-Control",
    cacheSeconds > 0
      ? `public, s-maxage=${cacheSeconds}, stale-while-revalidate=${cacheSeconds * 4}`
      : "no-store"
  );
  res.status(status).send(JSON.stringify(body));
}

export function fail(res, status, message) {
  json(res, status, { ok: false, error: message });
}

// timingSafeEqual throws on mismatched buffer lengths, so every caller here
// length-checks first — the length itself is not worth protecting via
// constant time, only the byte-for-byte comparison is.
function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  return timingSafeEqual(Buffer.from(a), Buffer.from(b));
}

// ── session cookie ──────────────────────────────────────────────────────
// Replaces sending EDIT_KEY as a header on every write (previously stored in
// the browser's localStorage, in plaintext, indefinitely). A stateless
// HMAC-signed cookie instead: log in once with the key, get a signed
// session token back, the raw key never sits in browser storage again.
//
// Signed with EDIT_KEY itself rather than a second secret — HMAC doesn't
// leak the key from its output, and a solo admin has one fewer required env
// var to remember to set. The real cost of this choice: revoking a leaked
// cookie early means rotating EDIT_KEY (which also kills header auth, same
// secret) — there is no server-side session store to revoke against
// individually. Mitigated with a short TTL (48h) instead of building
// epoch-based revocation, which would need a DB read on every authorized()
// call — and authorized() runs on every GET to set can_edit, which has to
// stay synchronous/zero-DB (that's the whole point of the refresh-gating
// fix elsewhere in this file's caller).
export const SESSION_COOKIE = "ds_session";
const SESSION_TTL_MS = 48 * 60 * 60 * 1000;

function signSession() {
  const key = process.env.EDIT_KEY;
  const payload = Buffer.from(JSON.stringify({ exp: Date.now() + SESSION_TTL_MS })).toString("base64url");
  const sig = createHmac("sha256", key).update(payload).digest("hex");
  return `${payload}.${sig}`;
}

function verifySession(token) {
  const key = process.env.EDIT_KEY;
  if (!key || typeof token !== "string") return false;
  const dot = token.indexOf(".");
  if (dot < 0) return false;
  const payload = token.slice(0, dot);
  const sig = token.slice(dot + 1);
  const expected = createHmac("sha256", key).update(payload).digest("hex");
  if (!constantTimeEqual(sig, expected)) return false;
  try {
    const { exp } = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return typeof exp === "number" && exp > Date.now();
  } catch {
    return false;
  }
}

// Manual parse — this deployment has no framework (@vercel/node bare
// functions), so req.cookies is never populated. Split on "; ", split each
// pair on the FIRST "=" only (a token can itself contain "="), last
// duplicate name wins.
function parseCookies(req) {
  const header = req.headers.cookie;
  const out = {};
  if (typeof header !== "string" || !header) return out;
  for (const part of header.split("; ")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    out[part.slice(0, eq).trim()] = part.slice(eq + 1).trim();
  }
  return out;
}

export function setSessionCookie(res) {
  res.setHeader(
    "Set-Cookie",
    `${SESSION_COOKIE}=${signSession()}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=${Math.floor(SESSION_TTL_MS / 1000)}`
  );
}

export function clearSessionCookie(res) {
  res.setHeader("Set-Cookie", `${SESSION_COOKIE}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0`);
}

// Writes are gated on a shared key. Fails closed: no EDIT_KEY set means the
// public internet cannot touch the ledger, which is the safe default for a
// public site sitting on top of a live trading database.
//
// Tries the session cookie first, falls back to the legacy x-edit-key
// header (still useful for curl/testing) — the fallback doesn't weaken
// anything, both paths require the same secret, and a custom header can't
// be attached by a cross-site request the way a cookie can, so it doesn't
// reopen CSRF either.
export function authorized(req) {
  const key = process.env.EDIT_KEY;
  if (!key) return false;
  const cookies = parseCookies(req);
  if (cookies[SESSION_COOKIE] && verifySession(cookies[SESSION_COOKIE])) return true;
  const sent = req.headers["x-edit-key"];
  return constantTimeEqual(sent, key);
}

// Login only — validates the raw key directly (constant-time), independent
// of authorized()'s cookie/header lookup. Exported so tracker.js's login
// action doesn't need its own copy of the comparison.
export function keyMatches(candidate) {
  const key = process.env.EDIT_KEY;
  if (!key) return false;
  return constantTimeEqual(candidate, key);
}

export async function readBody(req) {
  if (req.body && typeof req.body === "object") return req.body;
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    return {};
  }
}
