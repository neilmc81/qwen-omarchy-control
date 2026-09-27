// Gateway preload: do not request a speech response after a completed desktop
// action. This patches a public class at runtime, leaving npm files untouched.
// If a future qwen-audio-agent version changes the class, normal speech resumes
// rather than preventing the gateway from starting.
import { dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { shouldSilenceToolSuccess } from './qwen-silent-success-policy.mjs'

try {
  const entry = process.argv[1]
  if (entry?.endsWith('/server/src/index.mjs')) {
    const moduleUrl = pathToFileURL(resolve(dirname(entry), 'frontend/tools/tool-call-handler.mjs'))
    const { ToolCallHandler } = await import(moduleUrl.href)
    const original = ToolCallHandler.prototype.sendOutput
    if (typeof original === 'function') {
      ToolCallHandler.prototype.sendOutput = function (callId, output, turnId, taskId, options) {
        const name = this.activeToolEntries?.get(callId)?.name
        const responseOptions = shouldSilenceToolSuccess(name, output)
          ? { ...options, createResponse: false }
          : options
        return original.call(this, callId, output, turnId, taskId, responseOptions)
      }
    }
  }
} catch (error) {
  console.warn('Qwen silent-success preload unavailable:', error?.message || error)
}
