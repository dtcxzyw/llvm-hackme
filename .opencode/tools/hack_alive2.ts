import path from "node:path"
import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Run both baseline and PR opt on an IR file, then compare the results with alive2.  Returns whether the transformation is correct.",
  args: {
    ir_path: tool.schema
      .string()
      .describe("Relative path to the .ll file inside the hack work directory"),
    opt_args: tool.schema
      .string()
      .describe("Space-separated opt arguments passed to both baseline and PR opt"),
  },
  async execute(args) {
    const ctx = loadContext()
    const resolved = resolveConfined(args.ir_path, ctx.work_dir)
    const extra = parseArgs(args.opt_args)
    const baseOut = resolved + ".baseline.tgt.ll"
    const prOut = resolved + ".pr.tgt.ll"

    function memoryWrap(cmd: string[]): string[] {
      if (!ctx.opt_memory_limit_bytes) return cmd
      const prlimit = Bun.which("prlimit")
      if (!prlimit) return cmd
      return [prlimit, `--as=${ctx.opt_memory_limit_bytes}`, ...cmd]
    }

    const env = minimalEnv()

    const baseProc = Bun.spawnSync({
      cmd: memoryWrap([ctx.baseline_opt, "-S", "-o", baseOut, resolved, ...extra]),
      env,
      stdout: "pipe",
      stderr: "pipe",
    })
    if (baseProc.exitCode !== 0) {
      return JSON.stringify({
        baseline_crashed: true,
        baseline_stderr: new TextDecoder().decode(baseProc.stderr).slice(-4000),
        correct: false,
        miscompile: false,
      })
    }

    const prProc = Bun.spawnSync({
      cmd: memoryWrap([ctx.pr_opt, "-S", "-o", prOut, resolved, ...extra]),
      env,
      stdout: "pipe",
      stderr: "pipe",
    })
    if (prProc.exitCode !== 0) {
      tryCleanup(baseOut, prOut)
      return JSON.stringify({
        pr_crashed: true,
        pr_stderr: new TextDecoder().decode(prProc.stderr).slice(-4000),
        correct: false,
        miscompile: false,
      })
    }

    const aliveProc = Bun.spawnSync({
      cmd: memoryWrap([
        ctx.alive_tv,
        "--smt-to=10000",
        "--disable-undef-input",
        baseOut,
        prOut,
      ]),
      env,
      stdout: "pipe",
      stderr: "pipe",
    })
    tryCleanup(baseOut, prOut)

    const aliveOut = new TextDecoder().decode(aliveProc.stdout)
    const aliveErr = new TextDecoder().decode(aliveProc.stderr)
    const combined = aliveOut + aliveErr
    const correct =
      combined.includes("0 incorrect transformations") &&
      combined.includes("Transformation seems to be correct")
    const positive = /[1-9]\d* incorrect transformations?/.test(combined)
    const miscompile = !correct && positive

    return JSON.stringify({
      exit_code: aliveProc.exitCode,
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

function parseArgs(raw: string): string[] {
  if (!raw || !raw.trim()) return []
  return raw.trim().split(/\s+/)
}

function tryCleanup(...files: string[]): void {
  for (const f of files) {
    try {
      Bun.file(f).delete()
    } catch {}
  }
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
