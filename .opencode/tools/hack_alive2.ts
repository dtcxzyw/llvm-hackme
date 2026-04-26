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
    const baseline = Bun.resolveSync(args.baseline_ir_path, ctx.work_dir)
    const pr = Bun.resolveSync(args.pr_ir_path, ctx.work_dir)
    const proc = Bun.spawnSync({
      cmd: [
        ctx.alive_tv,
        "--smt-to=10000",
        "--disable-undef-input",
        baseline,
        pr,
      ],
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
    const miscompile =
      !correct &&
      (combined.includes("incorrect") ||
        combined.includes("ERROR") ||
        combined.includes("Transformation seems to be correct"))
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

function minimalEnv() {
  return {
    HOME: process.env.HOME || "",
    PATH: process.env.PATH || "",
    TMPDIR: process.env.TMPDIR || "/tmp",
    LANG: process.env.LANG || "C.UTF-8",
    LC_ALL: process.env.LC_ALL || "C.UTF-8",
  }
}
