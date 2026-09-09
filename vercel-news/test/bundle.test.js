// The API must not drag a native SQLite binary into every function.
//
// `@libsql/client` resolves to lib-esm/node.js, which statically imports
// ./sqlite3.js, which imports the `libsql` package — a loader whose only job
// is to pull @libsql/linux-x64-gnu / -musl, 18.8 MB of compiled binary. Vercel
// bundles per function and keeps the output of every deployment, and sixteen
// of the eighteen routes reach _db.js. That is what put Function Storage at
// 100% of the 10 GB free tier.
//
// Nothing here opens a local file. Turso is reached over HTTPS, `/web` is the
// same client without the sqlite3 path, and this test is what stops a future
// edit from quietly restoring the fat import while everything still works.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const API = join(dirname(fileURLToPath(import.meta.url)), "..", "api");
const files = readdirSync(API).filter((f) => f.endsWith(".js"));

test("no route imports the node build of @libsql/client", () => {
  const fat = [];
  for (const f of files) {
    const src = readFileSync(join(API, f), "utf8");
    // The bare specifier, or the explicit node/sqlite3 entrypoints. `/web`
    // and `/http` are fine — neither reaches the native package.
    if (/from\s+["']@libsql\/client(\/(node|sqlite3))?["']/.test(src)) fat.push(f);
  }
  assert.deepEqual(fat, [], `these pull the native binary: ${fat.join(", ")}`);
});

test("the shared client uses the web entrypoint", () => {
  const src = readFileSync(join(API, "_db.js"), "utf8");
  assert.match(src, /from\s+["']@libsql\/client\/web["']/);
});

test("the web client still accepts a Turso libsql:// URL", async () => {
  // The one behaviour the swap could have broken: /web refuses file: URLs,
  // and would refuse libsql: too if Turso's scheme were not in its set.
  const { createClient } = await import("@libsql/client/web");
  assert.doesNotThrow(() =>
    createClient({ url: "libsql://example.turso.io", authToken: "x" }));
  assert.throws(() => createClient({ url: "file:local.db" }));
});
