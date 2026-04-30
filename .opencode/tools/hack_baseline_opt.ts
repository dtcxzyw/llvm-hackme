import fs from "node:fs"
import path from "node:path"
import { tool } from "@opencode-ai/plugin"

const ENOSPC = 28

export default tool({
  description:
    "Run the baseline (unpatched) opt on LLVM IR text.  Returns exit code, signal, crashed, stdout, and stderr.  -S is always passed; do NOT add -S or -o flags to opt_args.",
  args: {
    ir: tool.schema
      .string()
      .describe("Full LLVM IR text of the module to test"),
    opt_args: tool.schema
      .string()
      .describe("Space-separated opt arguments, e.g. '-passes=instcombine<no-verify-fixpoint>'"),
  },
  async execute(args) {
    const ctx = loadContext()
    const extra = parseArgs(args.opt_args)

    const tmp = writeTemp(ctx.work_dir, args.ir)
    if (!tmp.startsWith("/")) return tmp

    const cmd: string[] = []
    if (ctx.opt_memory_limit_bytes) {
      const prlimit = Bun.which("prlimit")
      if (prlimit) cmd.push(prlimit, `--as=${ctx.opt_memory_limit_bytes}`)
    }
    cmd.push(ctx.baseline_opt, "-S", tmp, ...extra)

    const proc = Bun.spawnSync({ cmd, env: minimalEnv(), stdout: "pipe", stderr: "pipe" })
    try { fs.unlinkSync(tmp) } catch {}
    if (proc.exitCode === ENOSPC) {
      return JSON.stringify({ error: "disk_full", crashed: false })
    }
    const stdout = decodeBuf(proc.stdout)
    const stderr = decodeBuf(proc.stderr)
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
  return JSON.parse(fs.readFileSync(f, "utf-8"))
}

function writeTemp(workDir: string, ir: string): string {
  const f = path.join(workDir, `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.ll`)
  try {
    fs.writeFileSync(f, ir)
    return f
  } catch (e: any) {
    if (e?.code === "ENOSPC") return "disk_full"
    return `Failed to write temp file: ${e}`
  }
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

function decodeBuf(buf: any): string {
  if (typeof buf === "string") return buf
  if (Buffer.isBuffer(buf)) return buf.toString()
  if (buf instanceof Uint8Array || ArrayBuffer.isView(buf)) {
    return new TextDecoder().decode(buf)
  }
  return String(buf ?? "")
}
