// Only suppress a follow-up response for a verified, completed action.
// Questions, read-only tools, pending confirmations and failures still speak.
const DESKTOP_ACTIONS = new Set([
  'switch_workspace', 'focus_window', 'launch_app', 'launch_agent',
  'open_url', 'type_text', 'set_volume', 'volume_up', 'volume_down',
  'mute_audio', 'unmute_audio', 'move_active_window_to_workspace',
  'close_active_window', 'pointer_move', 'mouse_click', 'mouse_scroll',
  'click_element', 'do_gui_task', 'do_sequence', 'macro_replay',
  'browser_click', 'browser_open_visible_video', 'browser_type',
  'browser_navigate', 'browser_search', 'youtube_player_control',
  'confirm_pending', 'cancel_pending', 'watch_start', 'watch_stop',
])

export function shouldSilenceToolSuccess(toolName, output) {
  const name = String(toolName || '')
  const desktop = name.startsWith('mcp__qwen_omarchy_control__')
  const action = desktop ? name.slice('mcp__qwen_omarchy_control__'.length)
    : name === 'mcp__marketos__open_marketos' ? 'open_marketos' : ''
  if (action !== 'open_marketos' && !DESKTOP_ACTIONS.has(action)) return false
  if (output?.status !== 'ok' || output.error || !output.text) return false
  let result
  try { result = JSON.parse(output.text) } catch { return false }
  if (!result || typeof result !== 'object') return false
  if (result.pending || result.error || result.verified === false) return false
  if (result.verification && result.verification !== 'satisfied') return false
  if (result.status && !['done', 'ok', 'completed', 'running'].includes(result.status)) return false
  if (result.result && /^(no pending|launch requested|close sent)/i.test(result.result)) return false
  if (result.reason && /failed|could not|unavailable|unknown|not confirmed/i.test(result.reason)) return false
  return true
}
