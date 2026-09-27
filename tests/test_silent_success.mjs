import assert from 'node:assert/strict'
import { test } from 'node:test'
import { shouldSilenceToolSuccess as silent } from '../bin/qwen-silent-success-policy.mjs'

const result = value => ({ status: 'ok', text: JSON.stringify(value) })
const tool = name => `mcp__qwen_omarchy_control__${name}`

test('completed desktop actions are silent', () => {
  assert.equal(silent(tool('switch_workspace'), result({ result: 'switched to workspace 2' })), true)
  assert.equal(silent(tool('launch_app'), result({ result: 'launched files (window ready)' })), true)
  assert.equal(silent(tool('youtube_player_control'), result({ verified: true, state: {} })), true)
})

test('errors, unknown outcomes and confirmation requests remain spoken', () => {
  assert.equal(silent(tool('switch_workspace'), { status: 'error', error: true }), false)
  assert.equal(silent(tool('click_element'), result({ verified: false, verification: 'unknown' })), false)
  assert.equal(silent(tool('close_active_window'), result({ pending: 'close Chrome' })), false)
  assert.equal(silent(tool('do_gui_task'), result({ status: 'needs_agent' })), false)
})

test('questions and explicitly requested information remain spoken', () => {
  assert.equal(silent(tool('get_active_window'), result({ title: 'Home' })), false)
  assert.equal(silent('mcp__marketos__market_brief', result({ spoken: 'Market brief' })), false)
  assert.equal(silent(tool('watch_check'), result({ status: 'met', spoken: 'Done' })), false)
})
