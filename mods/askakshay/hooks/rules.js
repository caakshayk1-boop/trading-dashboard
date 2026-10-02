// Pure rules for the askakshay mod. No mods API here, so every rule is
// testable on its own and register.js stays the only file that reaches out.

// The repo this mod guards. A session elsewhere gets nothing from it.
export const MARKERS = ['generate.py', 'static/app.js', 'feeds']

// A path as the repo sees it: relative to the session's working directory.
export function rel(path, root) {
  if (typeof path !== 'string') return ''
  let p = path.replace(/\\/g, '/')
  const r = (root || '').replace(/\\/g, '/').replace(/\/+$/, '')
  if (r && p.startsWith(r + '/')) p = p.slice(r.length + 1)
  return p.replace(/^\.\//, '')
}

const CONFIG = 'config.py holds the API keys. CLAUDE.md says never read or modify it. ' +
  'Use config_template.py for its shape; the offline suites set placeholder env vars instead.'

// A file Claude is about to read or change. Returns { deny } for a refusal,
// { staticTwin, deny } when the refusal depends on static/<name> existing,
// or null to let the call through.
export function checkFile(tool, filePath, root) {
  const p = rel(filePath, root)
  if (!p) return null
  if (p === 'config.py') return { deny: CONFIG }
  if (tool === 'Read' || tool === 'Grep') return null
  if (p.startsWith('data/')) {
    return { deny: 'data/ is the raw market-data cache, written by the jobs. CLAUDE.md says never edit it by hand. Change the script that writes it.' }
  }
  const m = /^docs\/([^/]+)$/.exec(p)
  if (m) {
    return {
      staticTwin: 'static/' + m[1],
      deny: 'docs/' + m[1] + ' is a build artefact: generate.py copies static/' + m[1] + ' over it on every build, ' +
        'so an edit here is silently lost. Make the change in static/' + m[1] + '.',
    }
  }
  return null
}

// A shell command. Text matching is a reminder for Claude, not a security
// boundary: python -c, a recursive grep or a variable can still reach a file.
export function checkCommand(command) {
  if (typeof command !== 'string') return null
  if (/(?<![\w./-])(?:\.\/)?config\.py\b/.test(command)) return { deny: CONFIG }
  if (/\bgit\s+push\b[^;&|\n]*\s(?:\+?\S*:)?(?:refs\/heads\/)?(?:main|master)\b/.test(command)) {
    return { deny: 'Never push to main here. Push the claude/ working branch and open a pull request; deploys run from main.' }
  }
  return null
}

const money = (v) => v == null || !Number.isFinite(v) ? '—'
  : '₹' + v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const day = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00Z')
  return Number.isNaN(d.getTime()) ? iso : d.getUTCDate() + ' ' + ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getUTCMonth()]
}
const STATE = {
  awaiting_entry: 'waiting for a fill',
  open: 'filled',
  closed: 'closed',
  cancelled: 'cancelled before a fill',
  expired: 'lapsed unfilled',
}

// Whole days between the feed's as_of session and now. null when unreadable.
export function ageDays(asOf, nowMs) {
  const t = Date.parse((asOf || '') + 'T00:00:00Z')
  return Number.isFinite(t) && Number.isFinite(nowMs) ? Math.floor((nowMs - t) / 86400000) : null
}

// The paper block of feeds/signal_v2.json, in the words the Telegram digest
// uses. Every level comes from the feed; nothing here computes a signal.
export function paperText(feed, nowMs) {
  const P = feed && feed.paper
  if (!P || !Array.isArray(P.plans)) return 'feeds/signal_v2.json has no paper block.'
  const names = Object.fromEntries((P.engines || []).map((e) => [e.id, e.name]))
  const age = ageDays(P.as_of, nowMs)
  const head = 'Paper setups · feed as of ' + day(P.as_of) +
    (age != null && age > 4 ? ' (' + age + ' days old)' : '') + ' · paper test, not proven, not advice'
  if (!P.plans.length) return head + '\nNo paper setups open.'
  const lines = P.plans.map((p) => {
    const sp = p.sell_pct || [40, 35, 25]
    const st = STATE[p.state] || p.state || '—'
    const filled = p.fill_price != null
      ? ' · filled ' + money(p.fill_price) + (p.total_r != null ? ' · ' + (p.total_r > 0 ? '+' : '') + p.total_r.toFixed(2) + 'R' : '')
      : ''
    return '• ' + p.symbol + ' — ' + (names[p.engine] || p.engine) + ' · ' + st + filled + '\n' +
      '  Buy ' + money(p.entry_low) + '–' + money(p.entry_high) + ' from ' + day(p.for_session) + ' to ' + day(p.valid_through) +
      ' · stop ' + money(p.stop) + (p.risk_pct != null ? ' (' + p.risk_pct.toFixed(1) + '%)' : '') + '\n' +
      '  Sell ' + sp[0] + '% ' + money(p.t1) + ' · ' + sp[1] + '% ' + money(p.t2) + ' · ' + sp[2] + '% ' + money(p.t3) +
      (p.why ? '\n  Why: ' + p.why : '')
  })
  return head + '\n' + lines.join('\n')
}
