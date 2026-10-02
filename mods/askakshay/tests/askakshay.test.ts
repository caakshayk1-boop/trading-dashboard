import { expect, mock, test } from 'claude-code/testing'
import { checkCommand, checkFile, paperText } from '../hooks/rules.js'

const ROOT = '/work/trading-dashboard'
const DAY = 86400000
const AS_OF = Date.parse('2026-10-01T00:00:00Z')
const FEED = {
  paper: {
    as_of: '2026-10-01',
    engines: [{ id: 'sv2.technical_confluence', name: 'Technical Confluence' }],
    plans: [{
      engine: 'sv2.technical_confluence', symbol: 'WOCKPHARMA', for_session: '2026-10-05', valid_through: '2026-10-07',
      entry_low: 1995, entry_high: 2063, stop: 1927.4, t1: 2263.8, t2: 2350.9, t3: 2413.2, sell_pct: [39, 33, 28],
      state: 'awaiting_entry', fill_price: null, total_r: null, risk_pct: 6.6,
      why: 'scored 9/10 when recorded, in an uptrend (markup), 2.2R of room to resistance, RSI 48',
    }],
  },
}

// A session inside this repo: every marker file exists, static/ has app.js.
function inRepo(on, files = { 'static/app.js': true }, feed: unknown = FEED) {
  on('fs.exists', ($, e) => ({ value: ['generate.py', 'static/app.js', 'feeds'].some((m) => e.path.endsWith(m)) || Object.keys(files).some((f) => e.path.endsWith(f)) }))
  on('fs.read', ($, e) => ({ value: e.path.endsWith('feeds/signal_v2.json') ? JSON.stringify(feed) : '' }))
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: ROOT }))
  on('tool.call', () => ({ result: 'ran' }))
}

// ── the pure rules ───────────────────────────────────────────────────────
test('config.py is refused by path, and config_template.py is not', () => {
  expect(checkFile('Read', ROOT + '/config.py', ROOT)?.deny).toMatch(/API keys/)
  expect(checkFile('Read', ROOT + '/config_template.py', ROOT)).toBeNull()
  expect(checkFile('Read', ROOT + '/mods/config.py', ROOT)).toBeNull()
  expect(checkCommand('cat config.py')?.deny).toMatch(/API keys/)
  expect(checkCommand('sed -n 1,5p ./config.py')?.deny).toMatch(/API keys/)
  expect(checkCommand('diff config_template.py x')).toBeNull()
  expect(checkCommand('python3 scanner.py')).toBeNull()
})

test('a docs/ artefact, data/ and a push to main are refused; ordinary work is not', () => {
  expect(checkFile('Edit', ROOT + '/docs/app.js', ROOT)?.staticTwin).toBe('static/app.js')
  expect(checkFile('Read', ROOT + '/docs/app.js', ROOT)).toBeNull()
  expect(checkFile('Write', ROOT + '/data/alert_state.json', ROOT)?.deny).toMatch(/market-data cache/)
  expect(checkFile('Edit', ROOT + '/static/app.js', ROOT)).toBeNull()
  expect(checkCommand('git push origin main')?.deny).toMatch(/pull request/)
  expect(checkCommand('git push origin HEAD:main')?.deny).toMatch(/pull request/)
  expect(checkCommand('git push -u origin claude/signal-askakshay-redesign-14h6d2')).toBeNull()
  expect(checkCommand('git push origin maintenance-fix')).toBeNull()
})

test('the paper text carries every level from the feed and says it is paper', () => {
  const t = paperText(FEED, AS_OF + DAY)
  for (const s of ['WOCKPHARMA', 'Technical Confluence', 'waiting for a fill', '₹1,995.00–₹2,063.00', 'stop ₹1,927.40 (6.6%)',
    '39% ₹2,263.80', '28% ₹2,413.20', '5 Oct to 7 Oct', 'not proven']) expect(t).toContain(s)
  expect(t).not.toContain('days old')
  expect(paperText(FEED, AS_OF + 9 * DAY)).toContain('(9 days old)')
  expect(paperText({}, AS_OF)).toBe('feeds/signal_v2.json has no paper block.')
})

// ── the mod in a session ─────────────────────────────────────────────────
test('in this repo the guard refuses config.py and lets other reads run', async ($, on) => {
  mock.clock(on, { now: AS_OF + DAY })
  inRepo(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: ROOT })
  expect(await $.tool.call({ tool: 'Read', file_path: ROOT + '/config.py' })).toMatchObject({ deny: expect.stringMatching(/API keys/) })
  expect(await $.tool.call({ tool: 'Read', file_path: ROOT + '/scanner.py' })).toEqual({ result: 'ran' })
  expect(await $.tool.call({ tool: 'Bash', command: 'git push origin main' })).toMatchObject({ deny: expect.stringMatching(/pull request/) })
})

test('a docs/ file is refused only when static/ has its source', async ($, on) => {
  mock.clock(on, { now: AS_OF + DAY })
  inRepo(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: ROOT })
  expect(await $.tool.call({ tool: 'Edit', file_path: ROOT + '/docs/app.js' })).toMatchObject({ deny: expect.stringMatching(/static\/app\.js/) })
  expect(await $.tool.call({ tool: 'Write', file_path: ROOT + '/docs/screen.json' })).toEqual({ result: 'ran' })
})

test('outside this repo the mod does nothing at all', async ($, on) => {
  mock.clock(on, { now: AS_OF + DAY })
  on('fs.exists', () => ({ value: false }))
  on('session.start', () => ({ cwd: '/elsewhere' }))
  on('tool.call', () => ({ result: 'ran' }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/elsewhere' })
  expect(await $.tool.call({ tool: 'Read', file_path: '/elsewhere/config.py' })).toEqual({ result: 'ran' })
})

test('/paper prints the setups; a stale feed puts one line under the prompt', async ($, on) => {
  mock.clock(on, { now: AS_OF + 9 * DAY })
  const status: string[] = []
  on('ui.status', ($, e) => { status.push(e.text); return { value: undefined } })
  inRepo(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: ROOT })
  expect(status).toEqual(['paper feed is 9 days old — check eod.yml in vision-engine'])
  const out = await $.command.run({ command: 'paper', args: '' })
  expect(out.text).toContain('WOCKPHARMA')
})

test('a fresh feed is silent', async ($, on) => {
  mock.clock(on, { now: AS_OF + DAY })
  const status: string[] = []
  on('ui.status', ($, e) => { status.push(e.text); return { value: undefined } })
  inRepo(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: ROOT })
  expect(status).toEqual([])
})
