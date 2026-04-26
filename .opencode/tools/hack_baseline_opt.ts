import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Run the baseline (unpatched) opt on an IR file.  Returns stdout, stderr, exit code, and whether it crashed.",
  args: {
    ir_path: tool.schema
      .string()
      .describe("Relative path to the .ll file inside the hack work directory"),
    pass_name: tool.schema
      .string()
      .describe("opt pass pipeline, e.g. instcombine<no-verify-fixpoint>"),
  },
  async execute(args) {
    const ctx = loadContext()
    const resolved = Bun.resolveSync(args.ir_path, ctx.work_dir)
    const proc = Bun.spawnSync({
      cmd: [ctx.baseline_opt, "-S", "-o", "/dev/null", resolved, `-passes=${args.pass_name}`],
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

function minimalEnv() {
  return {
    HOME: process.env.HOME || "",
    PATH: process.env.PATH || "",
    TMPDIR: process.env.TMPDIR || "/tmp",
    LANG: process.env.LANG || "C.UTF-8",
    LC_ALL: process.env.LC_ALL || "C.UTF-8",
  }
}
