import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Read the hack session context (binary paths, pass name, work directories, patch file, and pipe paths).  Call this first.",
  args: {},
  async execute() {
    const contextFile = process.env.HACK_CONTEXT_FILE
    if (!contextFile) {
      return "HACK_CONTEXT_FILE not set"
    }
    try {
      const raw = await Bun.file(contextFile).text()
      const ctx = JSON.parse(raw)
      return JSON.stringify(ctx, null, 2)
    } catch (e) {
      return `Failed to read context file: ${e}`
    }
  },
})
