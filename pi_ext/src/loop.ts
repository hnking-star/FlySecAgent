import { AgentSession } from "@earendil-works/pi-coding-agent";
import type { ObserverResponse } from "./client.js";
import type { ObservationTools } from "./tools.js";
import { buildTurnPrompt } from "./system-prompt.js";

export interface RoundResult {
  ok: boolean;
  revision?: string | null;
  unchanged?: boolean | null;
  errors?: unknown;
}

const DEFAULT_MAX_ROUNDS = 10;
const DEFAULT_TIMEOUT_MS = 120_000;

export async function runOneRound(
  session: AgentSession,
  tools: ObservationTools,
  trigger: string,
  options: { maxRounds?: number; timeoutMs?: number } = {},
): Promise<RoundResult> {
  const maxRounds =
    options.maxRounds ?? Number(process.env.FLYSEC_PI_MAX_ROUNDS ?? DEFAULT_MAX_ROUNDS);
  const timeoutMs =
    options.timeoutMs ?? Number(process.env.FLYSEC_PI_TIMEOUT_MS ?? DEFAULT_TIMEOUT_MS);

  tools.reset();

  let toolCount = 0;
  let modelError: string | null = null;

  const unsubscribe = session.subscribe((ev) => {
    const kind = String((ev as { type?: string }).type || "");
    if (kind === "tool_execution_start") {
      toolCount += 1;
    }
    if (kind === "turn_end") {
      const message = (ev as { message?: { stopReason?: string; errorMessage?: string } })
        .message;
      if (message?.stopReason === "error") {
        modelError = message.errorMessage || "model request failed";
      }
    }
  });

  const timer = setTimeout(() => {
    void session.abort().catch(() => undefined);
  }, timeoutMs);

  let abortedByBudget: "rounds" | "timeout" | null = null;
  const budgetWatcher = setInterval(() => {
    const latest = tools.lastSubmit();
    if (latest?.ok === true) {
      void session.abort().catch(() => undefined);
      return;
    }
    if (toolCount >= maxRounds * 2 /* context + submit per round */) {
      abortedByBudget = "rounds";
      void session.abort().catch(() => undefined);
    }
  }, 500);

  try {
    await session.sendUserMessage(buildTurnPrompt(trigger));
    await session.waitForIdle();
  } finally {
    clearTimeout(timer);
    clearInterval(budgetWatcher);
    unsubscribe();
  }

  const finalSubmit = tools.lastSubmit();
  if (finalSubmit?.ok === true) {
    return {
      ok: true,
      revision: (finalSubmit.revision as string | null | undefined) ?? null,
      unchanged: Boolean(finalSubmit.unchanged),
    };
  }

  if (abortedByBudget === "rounds") {
    return {
      ok: false,
      errors: [
        {
          path: "/",
          code: "pi_round_exceeded",
          message: `exceeded ${maxRounds} tool rounds without ok:true`,
        },
      ],
    };
  }

  if (finalSubmit?.ok === false) {
    return { ok: false, errors: finalSubmit.errors };
  }

  if (modelError) {
    return {
      ok: false,
      errors: [{ path: "/", code: "pi_model_error", message: modelError }],
    };
  }

  return {
    ok: false,
    errors: [
      {
        path: "/",
        code: "no_submit",
        message:
          "model finished without calling observation_submit (check pi_ext logs)",
      },
    ],
  };
}
