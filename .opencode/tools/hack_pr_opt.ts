import path from "node:path"
import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Run the PR (patched) opt on an IR file.  Returns stdout, stderr, exit code, and whether it crashed.",
  args: {
    ir_path: tool.schema
      .string()
      .describe("Relative path to the .ll file inside the hack work directory"),
    opt_args: tool.schema
      .string()
      .describe("Space-separated opt arguments, e.g. '-passes=instcombine<no-verify-fixpoint>'"),
  },
  async execute(args) {
    const ctx = loadContext()
    const resolved = resolveConfined(args.ir_path, ctx.work_dir)
    const extra = parseArgs(args.opt_args)

    const cmd: string[] = []
    if (ctx.opt_memory_limit_bytes) {
      const prlimit = Bun.which("prlimit")
      if (prlimit) {
        cmd.push(prlimit, `--as=${ctx.opt_memory_limit_bytes}`)
      }
    }
    cmd.push(ctx.pr_opt, "-S", "-o", "/dev/null", resolved, ...extra)

    const proc = Bun.spawnSync({
      cmd,
      env: minimalEnv(),
      stdout: "pipe",
      stderr: "pipe",
    })
    const stdout = new TextDecoder().decode(proc.stdout)
    const stderr = new TextDecoder().decode(proc.stderr)
    const crashed = proc.exitCode !== 0
    return JSON.stringify({
      exit_code: proc.exitCode,
      signal: proc.signalCode,
      crashed,
      stdout: stdout.slice(-8000),
      stderr: stderr.slice(-8000),
    })
  },
})

function loadContext() {
  const f = process.env.HACK_CONTEXT_FILE
  if (!f) throw new Error("HACK_CONTEXT_FILE not set")
  return JSON.parse(new TextDecoder().decode(Bun.file(f).bytes()))
}

function resolveConfined(rel: string, base: string): string {
  if (!rel || !base) throw new Error("ir_path and work_dir are required")
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

function minimalEnv() {
  return {
    HOME: process.env.HOME || "",
    PATH: process.env.PATH || "",
    TMPDIR: process.env.TMPDIR || "/tmp",
    LANG: process.env.LANG || "C.UTF-8",
    LC_ALL: process.env.LC_ALL || "C.UTF-8",
  }
}
