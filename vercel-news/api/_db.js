// Shared Turso client + helpers for every /api route.
// TURSO_URL / TURSO_TOKEN are the same secrets the GitHub Actions scanner uses,
// so the site reads the exact ledger the bot writes — no copy, no drift.
import { createClient } from "@libsql/client";
import { timingSafeEqual } from "node:crypto";

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

const WIN = new Set(["TARGET_HIT", "T1_HIT", "T2_HIT", "TP1_HIT", "TP2_HIT", "PROFIT"]);
const LOSS = new Set(["SL_HIT", "STOPPED", "STOP_HIT", "LOSS"]);

// Mirrors fetch_alert_log() in newspaper.py so the live layer and the daily
// static build never disagree about what counts as a win.
export function badgeOf(status, lifecycle) {
  const s = str(status).toUpperCase();
  const lc = str(lifecycle).toUpperCase();
  if (WIN.has(s) || WIN.has(lc)) return "win";
  if (LOSS.has(s) || LOSS.has(lc)) return "loss";
  if (s === "OPEN" || lc === "OPEN") return "open";
  return "cancelled";
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

// Writes are gated on a shared key. Fails closed: no EDIT_KEY set means the
// public internet cannot touch the ledger, which is the safe default for a
// public site sitting on top of a live trading database.
//
// timingSafeEqual, because `===` on strings short-circuits at the first
// differing byte and leaks the key a character at a time to anyone willing to
// measure. The length is compared separately since timingSafeEqual throws on
// mismatched buffers — length alone is not worth protecting.
export function authorized(req) {
  const key = process.env.EDIT_KEY;
  if (!key) return false;
  const sent = req.headers["x-edit-key"];
  if (typeof sent !== "string" || sent.length !== key.length) return false;
  return timingSafeEqual(Buffer.from(sent), Buffer.from(key));
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
