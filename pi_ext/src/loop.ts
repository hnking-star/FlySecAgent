import type { AgentSession } from "@earendil-works/pi-coding-agent";
import type { CuratorTools } from "./tools.js";
import { buildTurnPrompt } from "./system-prompt.js";

export interface RoundResult {
  ok: boolean;
  revision?: string | null;
  unchanged?: boolean | null;
  errors?: unknown;
}

export async function runOneRound(
  session: AgentSession, tools: CuratorTools, trigger: string,
  options: { shouldStop?: () => boolean } = {},
): Promise<RoundResult> {
  tools.reset();
  let modelFailure: string | undefined;
  const unsubscribe = session.subscribe(event => {
    if (event.type === "message_end" && event.message.role === "assistant" && event.message.stopReason === "error") {
      modelFailure = event.message.errorMessage;
    }
  });
  // Submit success is authoritative. A natural-language final answer is not.
  let prompt = buildTurnPrompt(trigger);
  try { while (!options.shouldStop?.()) {
    try {
      await session.prompt(prompt);
      await session.waitForIdle();
    } catch {
      if (options.shouldStop?.()) break;
    }
    const result = tools.lastSubmit();
    if (result?.ok === true) {
      return { ok: true, revision: result.revision ?? null, unchanged: !result.published };
    }
    const rejection = modelFailure?.match(/^(400|401|402|403|404|422)\b/);
    if (rejection) return { ok: false, errors: [{ code: "model_request_rejected", path: "/", message: `Configured model rejected the request (${rejection[1]}). Check generated tool schemas, provider configuration or quota.` }] };
    if (tools.fatal()) return { ok: false, errors: [{ code: "runtime_stopped", path: "/", message: "Host stopped or revoked this memory runtime" }] };
    // Keep the same Pi session and fixed evidence window while repairing.
    prompt = "本轮尚未成功提交。请重新调用 curator_read 查看同一窗口，需要时回查证据；修正字段、引用或执行分类后再调用 curator_commit。正常观察和执行失败都要记录；无新记忆需要明确 unchanged_reason，不能因为未发现漏洞就跳过证据。";
    await new Promise(resolve => setTimeout(resolve, 1000));
  } } finally { unsubscribe(); }
  return { ok: false, errors: [{ code: "curator_cancelled", path: "/", message: "Memory Curator stopped" }] };
}
