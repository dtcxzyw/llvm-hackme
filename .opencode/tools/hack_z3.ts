import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Run Z3 on an SMT-LIB2 formula to search for counterexamples.  Memory limited to 4 GB, timeout 30 seconds.",
  args: {
    smtlib2: tool.schema.string().describe("SMT-LIB2 formula string"),
  },
  async execute(args) {
    const proc = Bun.spawnSync({
      cmd: ["z3", "-in", "-T:30", "-memory:4096"],
      stdin: Bun.plainText(args.smtlib2),
      stdout: "pipe",
      stderr: "pipe",
    })
    const stdout = new TextDecoder().decode(proc.stdout)
    const stderr = new TextDecoder().decode(proc.stderr)
    const combined = (stdout + stderr).trim()
    const sat = combined.includes("sat") && !combined.includes("unsat")
    const unsat = combined.includes("unsat")
    const timeout = combined.includes("timeout") || combined.includes("killed")
    const unknown = !sat && !unsat
    return JSON.stringify({
      sat,
      unsat,
      unknown,
      timeout,
      output: combined.slice(-12000),
    })
  },
})
