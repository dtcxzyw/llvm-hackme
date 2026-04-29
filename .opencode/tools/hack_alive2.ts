import fs from "node:fs"
import path from "node:path"
import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Check an @src / @tgt proof pair with alive2.  The IR must define both @src and @tgt functions.",
  args: {
    ir: tool.schema
      .string()
      .describe("Full LLVM IR text containing both @src and @tgt functions"),
  },
  async execute(args) {
    const ctx = loadContext()

    const tmp = writeTemp(ctx.work_dir, args.ir)
    if (!tmp.startsWith("/")) return tmp

    function memoryWrap(cmd: string[]): string[] {
      if (!ctx.opt_memory_limit_bytes) return cmd
      const prlimit = Bun.which("prlimit")
      if (!prlimit) return cmd
      return [prlimit, `--as=${ctx.opt_memory_limit_bytes}`, ...cmd]
    }

    const env = minimalEnv()

    const aliveProc = Bun.spawnSync({
      cmd: memoryWrap([ctx.alive_tv, "--smt-to=10000", "--disable-undef-input", tmp]),
      env, stdout: "pipe", stderr: "pipe",
    })
    tryCleanup(tmp)

    const aliveOut = decodeBuf(aliveProc.stdout)
    const aliveErr = decodeBuf(aliveProc.stderr)
    const combined = aliveOut + aliveErr
    const correct = combined.includes("0 incorrect transformations") &&
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
  return JSON.parse(fs.readFileSync(f, "utf-8"))
}

function writeTemp(workDir: string, ir: string): string {
  const f = path.join(
    workDir,
    `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.ll`,
  )
  try {
    fs.writeFileSync(f, ir)
    return f
  } catch (e: any) {
    if (e?.code === "ENOSPC") return "disk_full"
    return `Failed to write temp file: ${e}`
  }
}

function tryCleanup(...files: string[]): void {
  for (const f of files) {
    try { fs.unlinkSync(f) } catch {}
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

function decodeBuf(buf: any): string {
  if (typeof buf === "string") return buf
  if (Buffer.isBuffer(buf)) return buf.toString()
  if (buf instanceof Uint8Array || ArrayBuffer.isView(buf)) {
    return new TextDecoder().decode(buf)
  }
  return String(buf ?? "")
}
