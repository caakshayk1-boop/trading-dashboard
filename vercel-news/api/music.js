// /api/music — the liked-songs shelf.
//
//   GET                          list liked songs, newest first
//   POST {title,artist,url}      like a track            (requires x-edit-key)
//   POST {action:"unlike",...}   remove it               (requires x-edit-key)
//
// Reads are public, writes are gated on EDIT_KEY and fail closed — same
// contract as /api/tracker. This page is on the public internet sitting over a
// live database; an anonymous visitor must not be able to write to it.
//
// Why a table and not a file: music.py owns the three built-in crates and is
// edited by hand for the 6 AM build. Likes arrive one tap at a time from a
// phone, so they need a store that takes a single row without a rebuild. The
// crates stay declarative; the likes are data.
import { db, str, json, fail, authorized, readBody } from "./_db.js";

export default async function handler(req, res) {
  try {
    if (req.method !== "GET" && req.method !== "POST") {
      return fail(res, 405, "GET or POST only");
    }
    // Reject unauthorized writes before touching the database at all.
    if (req.method === "POST" && !authorized(req)) {
      return fail(
        res,
        401,
        process.env.EDIT_KEY
          ? "Wrong edit key."
          : "Writes are disabled — EDIT_KEY is not set on this deployment."
      );
    }
    await ensureTable();
    return req.method === "GET" ? await list(req, res) : await write(req, res);
  } catch (e) {
    return fail(res, 500, `music failed: ${e.message}`);
  }
}

async function ensureTable() {
  await db().execute(`CREATE TABLE IF NOT EXISTS liked_songs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, artist TEXT, url TEXT,
    crate TEXT, liked_at TEXT
  )`);
  // A track can be liked from any crate and from any device. Without this the
  // same song lands in the shelf once per tap, and a double-tap on a phone
  // (which is easy to do on a 44px control) silently duplicates it.
  await db().execute(
    `CREATE UNIQUE INDEX IF NOT EXISTS liked_songs_uniq
       ON liked_songs (title, artist)`
  );
}

async function list(req, res) {
  const rs = await db().execute(
    `SELECT id, title, artist, url, crate, liked_at
       FROM liked_songs ORDER BY liked_at DESC, id DESC`
  );
  const songs = rs.rows.map((r) => ({
    id: Number(r.id),
    title: str(r.title),
    artist: str(r.artist),
    url: str(r.url),
    crate: str(r.crate),
    liked_at: str(r.liked_at),
  }));
  return json(res, 200, { ok: true, count: songs.length, songs });
}

async function write(req, res) {
  const body = await readBody(req);
  const action = str(body.action).toLowerCase();
  const title = str(body.title).trim();
  const artist = str(body.artist).trim();

  if (!title) return fail(res, 400, "title is required");

  if (action === "unlike") {
    await db().execute({
      sql: `DELETE FROM liked_songs WHERE title = ? AND artist = ?`,
      args: [title, artist],
    });
    return json(res, 200, { ok: true, liked: false, title, artist });
  }

  // INSERT OR IGNORE against the unique index above, so liking a track that is
  // already on the shelf is a no-op rather than an error — the button is a
  // toggle and the user should never see a failure for tapping it twice.
  await db().execute({
    sql: `INSERT OR IGNORE INTO liked_songs (title, artist, url, crate, liked_at)
          VALUES (?, ?, ?, ?, ?)`,
    args: [
      title,
      artist,
      str(body.url),
      str(body.crate),
      new Date().toISOString(),
    ],
  });
  return json(res, 200, { ok: true, liked: true, title, artist });
}
