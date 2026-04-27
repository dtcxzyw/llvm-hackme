import path from "node:path"
import { tool } from "@opencode-ai/plugin"

const ENOSPC = 28

export default tool({
  description:
    "Run both baseline and PR opt on LLVM IR text, then compare the results with alive2.",
  args: {
    ir: tool.schema
      .string()
      .describe("Full LLVM IR text of the module to test"),
    opt_args: tool.schema
      .string()
      .describe("Space-separated opt arguments passed to both baseline and PR opt"),
  },
  async execute(args) {
    const ctx = loadContext()
    const extra = parseArgs(args.opt_args)

    const tmp = writeTemp(ctx.work_dir, args.ir)
    if (typeof tmp === "string") return tmp

    const baseOut = tmp + ".baseline.tgt.ll"
    const prOut = tmp + ".pr.tgt.ll"

    function memoryWrap(cmd: string[]): string[] {
      if (!ctx.opt_memory_limit_bytes) return cmd
      const prlimit = Bun.which("prlimit")
      if (!prlimit) return cmd
      return [prlimit, `--as=${ctx.opt_memory_limit_bytes}`, ...cmd]
    }

    const env = minimalEnv()

    const baseProc = Bun.spawnSync({
      cmd: memoryWrap([ctx.baseline_opt, "-S", "-o", baseOut, tmp, ...extra]),
      env, stdout: "pipe", stderr: "pipe",
    })
    if (baseProc.exitCode !== 0 && baseProc.exitCode !== ENOSPC) {
      tryCleanup(tmp, baseOut, prOut)
      return JSON.stringify({ baseline_crashed: true, baseline_stderr: new TextDecoder().decode(baseProc.stderr).slice(-4000), correct: false, miscompile: false })
    }
    if (baseProc.exitCode === ENOSPC) { tryCleanup(tmp, baseOut, prOut); return JSON.stringify({ error: "disk_full" }) }

    const prProc = Bun.spawnSync({
      cmd: memoryWrap([ctx.pr_opt, "-S", "-o", prOut, tmp, ...extra]),
      env, stdout: "pipe", stderr: "pipe",
    })
    if (prProc.exitCode !== 0 && prProc.exitCode !== ENOSPC) {
      tryCleanup(tmp, baseOut, prOut)
      return JSON.stringify({ pr_crashed: true, pr_stderr: new TextDecoder().decode(prProc.stderr).slice(-4000), correct: false, miscompile: false })
    }
    if (prProc.exitCode === ENOSPC) { tryCleanup(tmp, baseOut, prOut); return JSON.stringify({ error: "disk_full" }) }

    const aliveProc = Bun.spawnSync({
      cmd: memoryWrap([ctx.alive_tv, "--smt-to=10000", "--disable-undef-input", baseOut, prOut]),
      env, stdout: "pipe", stderr: "pipe",
    })
    tryCleanup(tmp, baseOut, prOut)

    const aliveOut = new TextDecoder().decode(aliveProc.stdout)
    const aliveErr = new TextDecoder().decode(aliveProc.stderr)
    const combined = aliveOut + aliveErr
    const correct = combined.includes("0 incorrect transformations") && combined.includes("Transformation seems to be correct")
    const positive = /[1-9]\d* incorrect transformations?/.test(combined)
    const miscompile = !correct && positive

    return JSON.stringify({ exit_code: aliveProc.exitCode, correct, miscompile, counterexample: combined.slice(-8000) })
  },
})

function loadContext() {
  const f = process.env.HACK_CONTEXT_FILE
  if (!f) throw new Error("HACK_CONTEXT_FILE not set")
  return JSON.parse(new TextDecoder().decode(Bun.file(f).bytes()))
}

function writeTemp(workDir: string, ir: string): string | undefined {
  const f = path.join(workDir, `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.ll`)
  try { Bun.writeSync(f, ir); return f } catch (e: any) {
    if (e?.code === "ENOSPC") return "disk_full"
    return `Failed to write temp file: ${e}`
  }
}

function parseArgs(raw: string): string[] {
  if (!raw || !raw.trim()) return []
  return raw.trim().split(/\s+/)
}

function tryCleanup(...files: string[]): void {
  for (const f of files) { try { Bun.file(f).delete() } catch {} }
}

function minimalEnv() {
  return { HOME: process.env.HOME || "", PATH: process.env.PATH || "", TMPDIR: process.env.TMPDIR || "/tmp", LANG: process.env.LANG || "C.UTF-8", LC_ALL: process.env.LC_ALL || "C.UTF-8" }
}
