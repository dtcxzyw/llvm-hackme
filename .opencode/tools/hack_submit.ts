import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Submit a candidate reproducer to the Python service for verification.  If the bug is confirmed, the process exits.  Otherwise returns the verification failure reason so you can retry.",
  args: {
    ir: tool.schema
      .string()
      .describe("Full LLVM IR text of the reproducer"),
    pass_name: tool.schema
      .string()
      .describe("opt pass pipeline, e.g. instcombine<no-verify-fixpoint>"),
    kind: tool.schema
      .string()
      .describe('Bug kind: "crash" or "miscompilation"'),
    description: tool.schema
      .string()
      .describe("One-line description of the bug"),
  },
  async execute(args) {
    const submitPipe = process.env.HACK_SUBMIT_PIPE
    const responsePipe = process.env.HACK_RESPONSE_PIPE
    if (!submitPipe || !responsePipe) {
      return "HACK_SUBMIT_PIPE or HACK_RESPONSE_PIPE not set"
    }

    const payload = JSON.stringify({
      ir: args.ir,
      pass_name: args.pass_name,
      kind: args.kind,
      description: args.description,
    })

    try {
      const submitFd = Bun.openSync(submitPipe, { flags: "w" })
      Bun.writeSync(submitFd, payload + "\n")
      Bun.closeSync(submitFd)

      const respFd = Bun.openSync(responsePipe, { flags: "r" })
      const raw = Bun.readFileSync(respFd).toString()
      Bun.closeSync(respFd)

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
