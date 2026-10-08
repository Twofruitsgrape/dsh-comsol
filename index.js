/**
 * dsh-comsol - registers the comsol-dsh agent skill.
 *
 * The skill body, its runners (scripts/run_workflow.py, scripts/run_transient.py)
 * and the comsol-mcp engine ship inside this bundle:
 *   assets/comsol-dsh/   the skill (SKILL.md + scripts + tests)
 *   assets/engine/       the vendored comsol-mcp checkout (src/ + tests/)
 *
 * The skill name/description are parsed from the SKILL.md frontmatter at load
 * time, so the plugin manifest and the skill body can never drift apart -
 * SKILL.md is the single source of truth. `get` re-reads the body, so an
 * on-disk update is picked up after a plugin reload.
 *
 * Toggling this plugin in the DSH plugin panel enables/disables the skill for
 * every workspace at once - the plugin panel is the switch.
 */

import { readFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const PROVIDER_NAME = 'dsh-comsol'
const SKILL_DIR = new URL('./assets/comsol-dsh/', import.meta.url)
const SKILL_URL = new URL('SKILL.md', SKILL_DIR)
const RESOURCE_BASE = { kind: 'directory', path: fileURLToPath(SKILL_DIR) }

const FALLBACK = {
  name: 'comsol-dsh',
  description:
    'Drive COMSOL Multiphysics models end to end. Use when the user asks to ' +
    'run, modify, solve, or inspect a COMSOL (.mph) model.',
}

/** Minimal YAML frontmatter reader: top-level `key: value` lines. */
function parseFrontmatter (text) {
  const out = {}
  if (!text.startsWith('---')) return out
  const end = text.indexOf('\n---', 3)
  if (end < 0) return out
  for (const line of text.slice(3, end).split('\n')) {
    const m = /^([A-Za-z][\w-]*):\s*(.*)$/.exec(line.trim())
    if (m && out[m[1]] === undefined) out[m[1]] = m[2].trim()
  }
  return out
}

async function skillMeta () {
  try {
    const text = await readFile(SKILL_URL, 'utf8')
    const fm = parseFrontmatter(text)
    return {
      name: fm.name || FALLBACK.name,
      description: fm.description || FALLBACK.description,
    }
  } catch {
    return { ...FALLBACK }
  }
}

const INVOCATION = { modelInvocable: true, userInvocable: true }

const provider = {
  name: PROVIDER_NAME,
  async list () {
    if (!existsSync(fileURLToPath(SKILL_URL))) return []
    const meta = await skillMeta()
    return [{
      name: meta.name,
      description: meta.description,
      invocation: INVOCATION,
      provider: PROVIDER_NAME,
      source: 'bundled',
      resourceBase: RESOURCE_BASE,
      locator: fileURLToPath(SKILL_URL),
    }]
  },
  async get (candidate) {
    const meta = await skillMeta()
    return {
      name: candidate?.name || meta.name,
      description: meta.description,
      invocation: INVOCATION,
      provider: PROVIDER_NAME,
      source: 'bundled',
      resourceBase: RESOURCE_BASE,
      content: await readFile(SKILL_URL, 'utf8'),
    }
  },
}

/** Cordis plugin name. */
export const name = 'comsol'
/** Services this plugin requires; the skill registry injects them. */
export const inject = ['skills']

/** @param {any} ctx */
export function apply (ctx) {
  if (!existsSync(fileURLToPath(SKILL_URL))) {
    ctx?.logger?.warn?.('dsh-comsol: assets/comsol-dsh/SKILL.md missing; skill not registered')
    return
  }
  ctx.skills.registerProvider(() => provider)
}
