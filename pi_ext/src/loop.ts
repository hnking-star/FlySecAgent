import type { AgentSession } from "@earendil-works/pi-coding-agent";
import type { ObservationTools } from "./tools.js";
import { buildTurnPrompt } from "./system-prompt.js";

export interface RoundResult {
  ok: boolean;
  revision?: string | null;
  unchanged?: boolean | null;
  errors?: unknown;
}

export async function runOneRound(
  session: AgentSession, tools: ObservationTools, trigger: string,
  options: { shouldStop?: () => boolean } = {},
): Promise<RoundResult> {
  tools.reset();
  // Submit success is authoritative. A natural-language final answer is not.
  let prompt = buildTurnPrompt(trigger);
  while (!options.shouldStop?.()) {
    try {
      await session.prompt(prompt);
      await session.waitForIdle();
    } catch {
      if (options.shouldStop?.()) break;
    }
    const result = tools.lastSubmit();
    if (result?.ok === true) {
      return { ok: true, revision: result.revision ?? null, unchanged: Boolean(result.unchanged) };
    }
    // Keep the same Pi session and fixed evidence window while repairing.
    prompt = "本轮尚未成功提交。请重新调用 observation_context 查看同一窗口，需要时回查证据；修正工具返回的问题后再调用 observation_submit。正常结果和失败尝试也要总结，不得用空提交跳过未处理的内容。";
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  return { ok: false, errors: [{ code: "observer_cancelled", path: "/", message: "Observer stopped" }] };
}
