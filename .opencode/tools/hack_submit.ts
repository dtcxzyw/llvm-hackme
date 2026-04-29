import fs from "node:fs"
import { tool } from "@opencode-ai/plugin"

const MAX_IR_BYTES = 10 * 1024 * 1024

export default tool({
  description:
    "Submit a candidate reproducer to the Python service for verification.  If the bug is confirmed, the process exits.  Otherwise returns the verification failure reason so you can retry.",
  args: {
    ir: tool.schema
      .string()
      .describe("Full LLVM IR text of the reproducer"),
    opt_args: tool.schema
      .string()
      .describe("Space-separated opt arguments that trigger the bug, e.g. '-passes=instcombine<no-verify-fixpoint>'"),
    kind: tool.schema
      .string()
      .describe('Bug kind: "crash" or "miscompilation"'),
    description: tool.schema
      .string()
      .describe("One-line description of the bug"),
    alive2_args: tool.schema
      .string()
      .describe("Optional extra alive-tv flags, e.g. '-src-unroll=4 -tgt-unroll=4' (max unroll 128)"),
  },
  async execute(args) {
    const irBytes = new TextEncoder().encode(args.ir).length
    if (irBytes > MAX_IR_BYTES) {
      return `IR too large (${(irBytes / 1024 / 1024).toFixed(1)} MB).  Limit is ${MAX_IR_BYTES / 1024 / 1024} MB.  Reduce the test case.`
    }

    const submitPipe = process.env.HACK_SUBMIT_PIPE
    const responsePipe = process.env.HACK_RESPONSE_PIPE
    if (!submitPipe || !responsePipe) {
      return "HACK_SUBMIT_PIPE or HACK_RESPONSE_PIPE not set"
    }

    const payload = JSON.stringify({
      ir: args.ir,
      opt_args: args.opt_args,
      kind: args.kind,
      description: args.description,
      alive2_args: args.alive2_args || "",
    })

    try {
      const data = new TextEncoder().encode(payload + "\n")
      const wfd = fs.openSync(submitPipe, "w")
      fs.writeSync(wfd, data)
      fs.closeSync(wfd)

      const rfd = fs.openSync(responsePipe, "r")
      const buf = Buffer.alloc(65536)
      const n = fs.readSync(rfd, buf, 0, buf.length)
      fs.closeSync(rfd)
      const raw = buf.toString("utf-8", 0, n).trim()

      const resp = JSON.parse(raw)
      if (resp.success) {
        return "Bug confirmed and reported.  Exiting."
      }
      return `Verification failed: ${resp.reason || "unknown reason"}.  You may retry.`
    } catch (e: any) {
      if (e?.code === "ENXIO") {
        return "Response pipe closed before reading — the Python service may have terminated.  If you submitted a valid reproducer, it may have been accepted."
      }
      return `Submit failed: ${e}`
    }
  },
})
