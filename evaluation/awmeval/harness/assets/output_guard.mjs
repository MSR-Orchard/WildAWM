import { Buffer } from "node:buffer"

const LIVE_TOOL_OUTPUT_TOTAL_BYTES = 24 * 1024
const LIVE_TOOL_OUTPUT_PER_RESULT_BYTES = 4 * 1024
const INSTALL_KEY = Symbol.for("awmeval.outputGuard.installed")

function prefixBytes(value, limit) {
  if (limit <= 0) return ""
  const bytes = Buffer.from(value, "utf8")
  if (bytes.length <= limit) return value
  let result = bytes.subarray(0, limit).toString("utf8")
  while (Buffer.byteLength(result, "utf8") > limit) result = result.slice(0, -1)
  return result
}

function textOutput(output) {
  if (typeof output.output === "string") {
    return {
      value: output.output,
      replace(value) {
        output.output = value
      },
    }
  }
  if (!Array.isArray(output.content)) return undefined
  const indexes = output.content.flatMap((item, index) => item?.type === "text" && typeof item.text === "string" ? [index] : [])
  if (!indexes.length) return undefined
  return {
    value: indexes.map((index) => output.content[index].text).join("\n\n"),
    replace(value) {
      const textIndexes = new Set(indexes)
      let replaced = false
      output.content = output.content.flatMap((item, index) => {
        if (!textIndexes.has(index)) return [item]
        if (replaced) return []
        replaced = true
        return [{ ...item, text: value }]
      })
    },
  }
}

const LiveToolOutputGuard = async () => {
  if (globalThis[INSTALL_KEY]) return {}
  globalThis[INSTALL_KEY] = true
  const remainingBySession = new Map()
  return {
    "chat.params": async (input) => {
      remainingBySession.set(input.sessionID, LIVE_TOOL_OUTPUT_TOTAL_BYTES)
    },
    "tool.execute.after": async (input, output) => {
      const text = textOutput(output)
      if (!text) return
      const original = text.value
      const originalBytes = Buffer.byteLength(original, "utf8")
      const remaining = Math.max(0, remainingBySession.get(input.sessionID) ?? LIVE_TOOL_OUTPUT_TOTAL_BYTES)
      const allowance = Math.min(LIVE_TOOL_OUTPUT_PER_RESULT_BYTES, remaining)
      let guarded = original
      if (originalBytes > allowance) {
        const location = input.tool === "read" && typeof input.args?.filePath === "string" ? ` Use read with offset/limit or grep on ${input.args.filePath}.` : " Use a narrower or paginated tool call."
        const marker = `\n\n[Output truncated by the live context guard; ${originalBytes} original bytes.${location}]`
        const markerBytes = Buffer.byteLength(marker, "utf8")
        guarded = allowance > markerBytes ? prefixBytes(original, allowance - markerBytes) + marker : prefixBytes(marker, allowance)
      }
      const emittedBytes = Buffer.byteLength(guarded, "utf8")
      remainingBySession.set(input.sessionID, Math.max(0, remaining - emittedBytes))
      text.replace(guarded)
      output.metadata = {
        ...(output.metadata ?? {}),
        liveToolOutputGuard: {
          originalBytes,
          emittedBytes,
          truncated: originalBytes !== emittedBytes,
          remainingBytes: Math.max(0, remaining - emittedBytes),
          totalBudgetBytes: LIVE_TOOL_OUTPUT_TOTAL_BYTES,
          perResultBudgetBytes: LIVE_TOOL_OUTPUT_PER_RESULT_BYTES,
        },
      }
    },
  }
}

export default LiveToolOutputGuard
