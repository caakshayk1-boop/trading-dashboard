// askakshay — a Claude Code mod for the trading-dashboard repo.
//
// 1. A guard for the rules CLAUDE.md states in words: never read or edit
//    config.py, never hand-edit data/, never edit a docs/ file that
//    generate.py copies from static/, never push to main. Words in a
//    CLAUDE.md are advice; a refused tool call is not.
// 2. /paper — the paper setups from feeds/signal_v2.json in plain words.
// 3. A status line only when that feed is more than four days old. Silent
//    on success, loud on failure, the same shape as health_watch.py.
//
// Nothing runs outside this repo: session.start looks for its files first.
import { MARKERS, checkCommand, checkFile, paperText, ageDays } from './rules.js'

let root = ''
let active = false

async function readFeed($) {
  try {
    return JSON.parse(await $.fs.read('feeds/signal_v2.json'))
  } catch {
    return null
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    root = e.cwd || ''
    let found = 0
    for (const m of MARKERS) if (await $.fs.exists(m)) found += 1
    active = found === MARKERS.length
    if (active) {
      const feed = await readFeed($)
      const age = feed && feed.paper ? ageDays(feed.paper.as_of, await $.clock.now()) : null
      if (age != null && age > 4) $.ui.status('paper feed is ' + age + ' days old — check eod.yml in vision-engine')
      try {
        await $.command.register({ name: 'paper', description: 'The paper setups from feeds/signal_v2.json, in plain words' })
      } catch {
        // A name already taken: the guard still runs.
      }
    }
    return next(e)
  })

  on('tool.call', { tool: ['Read', 'Edit', 'Write', 'NotebookEdit'] }, async ($, e, next) => {
    if (!active) return next(e)
    const hit = checkFile(e.tool, e.file_path || e.notebook_path, root)
    if (!hit) return next(e)
    if (hit.staticTwin && !(await $.fs.exists(hit.staticTwin))) return next(e)
    return { deny: hit.deny }
  }).catch(async () => ({ deny: 'The askakshay file guard failed, so this call was not run. Retry, or turn the mod off in /plugin.' }))

  on('tool.call', { tool: 'Grep' }, async ($, e, next) => {
    if (!active) return next(e)
    const hit = checkFile('Grep', e.path, root)
    return hit ? { deny: hit.deny } : next(e)
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (!active) return next(e)
    const hit = checkCommand(e.command)
    return hit ? { deny: hit.deny } : next(e)
  }).catch(async () => ({ deny: 'The askakshay command guard failed, so this command was not run.' }))

  on('command.run', { command: 'paper' }, async ($) => {
    const feed = await readFeed($)
    if (!feed) return { text: 'feeds/signal_v2.json could not be read from this directory.' }
    return { text: paperText(feed, await $.clock.now()) }
  })
}
