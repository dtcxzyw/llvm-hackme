import path from "node:path"
import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Run alive2 to compare two IR files: a baseline-optimized output and a PR-optimized output.  Returns whether the transformation is correct.",
  args: {
    baseline_ir_path: tool.schema
      .string()
      .describe("Relative path to the .ll file produced by baseline opt"),
    pr_ir_path: tool.schema
      .string()
      .describe("Relative path to the .ll file produced by PR opt"),
  },
  async execute(args) {
    const ctx = loadContext()
    const baseline = resolveConfined(args.baseline_ir_path, ctx.work_dir)
    const pr = resolveConfined(args.pr_ir_path, ctx.work_dir)
    const cmd: string[] = []
    if (ctx.opt_memory_limit_bytes) {
      const prlimit = Bun.which("prlimit")
      if (prlimit) {
        cmd.push(prlimit, `--as=${ctx.opt_memory_limit_bytes}`)
      }
    }
    cmd.push(
      ctx.alive_tv,
      "--smt-to=10000",
      "--disable-undef-input",
      baseline,
      pr,
    )
    const proc = Bun.spawnSync({
      cmd,
      env: minimalEnv(),
      stdout: "pipe",
      stderr: "pipe",
    })
    const stdout = new TextDecoder().decode(proc.stdout)
    const stderr = new TextDecoder().decode(proc.stderr)
    const combined = stdout + stderr
    const correct =
      combined.includes("0 incorrect transformations") &&
      combined.includes("Transformation seems to be correct")
    const positive_incorrect = /[1-9]\d* incorrect transformations?/.test(combined)
    const miscompile = !correct && positive_incorrect
    return JSON.stringify({
      exit_code: proc.exitCode,
      correct,
      miscompile,
      counterexample: combined.slice(-8000),
    })
  },
})

function loadContext() {
  const f = process.env.HACK_CONTEXT_FILE
  if (!f) throw new Error("HACK_CONTEXT_FILE not set")
  return JSON.parse(new TextDecoder().decode(Bun.file(f).bytes()))
}

function resolveConfined(rel: string, base: string): string {
  if (!rel || !base) throw new Error("path arguments are required")
  const resolved = path.resolve(base, rel)
  const sep = path.sep
  if (resolved !== base && !resolved.startsWith(base + sep)) {
    throw new Error(`Path "${rel}" escapes work directory`)
  }
  return resolved
}

function minimalEnv() {
  return {
    HOME: process.env.HOME || "",
    PATH: process.env.PATH || "",
    TMPDIR: process.env.TMPDIR || "/tmp",
    LANG: process.env.LANG || "C.UTF-8",
    LC_ALL: process.env.LC_ALL || "C.UTF-8",
  }
}
