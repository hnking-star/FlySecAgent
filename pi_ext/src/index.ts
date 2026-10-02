import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";
import {
  createAgentSession,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import { ObserverClient } from "./client.js";
import { runOneRound, type RoundResult } from "./loop.js";
import { loadSystemPrompt } from "./system-prompt.js";
import { createObservationTools } from "./tools.js";
import type { RpcIn, RpcOut } from "./types.js";

function send(msg: RpcOut): void {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

function requireEnv(name: string): string {
  const v = process.env[name];
  if (!v) {
    process.stderr.write(`[pi_ext] missing env ${name}\n`);
    process.exit(2);
  }
  return v;
}

function seedModelsDoc(
  agentDir: string,
  provider: string,
  modelId: string,
  baseUrl: string,
): void {
  const modelsPath = join(agentDir, "models.json");
  if (existsSync(modelsPath)) return;
  const api = baseUrl.toLowerCase().includes("/anthropic")
    ? "anthropic-messages"
    : "openai-completions";
  const providerBlock: Record<string, unknown> = {
    api,
    apiKey: api === "anthropic-messages" ? "$ANTHROPIC_API_KEY" : "$OPENAI_API_KEY",
    baseUrl,
    models: [
      {
        id: modelId,
        name: modelId,
        reasoning: api === "openai-completions",
        input: ["text"],
        contextWindow: 128_000,
        maxTokens: api === "anthropic-messages" ? 16384 : 32768,
      },
    ],
  };
  const doc = { providers: { [provider]: providerBlock } };
  writeFileSync(modelsPath, JSON.stringify(doc, null, 2) + "\n", "utf8");
}

async function main(): Promise<void> {
  const token = requireEnv("FLYSEC_TOKEN");
  const api = requireEnv("FLYSEC_API");
  const sessionId = process.env.FLYSEC_SESSION_ID ?? "unknown";

  const provider = (process.env.FLYSEC_PI_PROVIDER || "anthropic").trim();
  const modelId = (process.env.FLYSEC_PI_MODEL || "deepseek-flash").trim();
  const apiKey = (process.env.FLYSEC_PI_API_KEY || "").trim();
  const baseUrl = (process.env.FLYSEC_PI_BASE_URL || "").trim();
  const agentDir =
    process.env.FLYSEC_PI_AGENT_DIR?.trim() ||
    join(process.cwd(), "data", "pi", sessionId, ".pi");

  process.env.PI_OFFLINE = process.env.PI_OFFLINE || "1";
  process.env.PI_CODING_AGENT_DIR = agentDir;
  if (baseUrl) {
    process.env.ANTHROPIC_BASE_URL = baseUrl;
    process.env.OPENAI_BASE_URL = baseUrl;
  }
  if (apiKey) {
    if (!process.env.ANTHROPIC_API_KEY) process.env.ANTHROPIC_API_KEY = apiKey;
    if (!process.env.ANTHROPIC_AUTH_TOKEN) process.env.ANTHROPIC_AUTH_TOKEN = apiKey;
    if (!process.env.OPENAI_API_KEY) process.env.OPENAI_API_KEY = apiKey;
  }

  mkdirSync(agentDir, { recursive: true });
  if (baseUrl) {
    seedModelsDoc(agentDir, provider, modelId, baseUrl);
  }

  const client = new ObserverClient(api, token);
  const observationTools = createObservationTools(client);
  const systemPrompt = loadSystemPrompt();

  const modelRuntime = await ModelRuntime.create({
    authPath: join(agentDir, "auth.json"),
    modelsPath: join(agentDir, "models.json"),
    refreshOnCreate: false,
    allowModelNetwork: false,
  });

  if (apiKey) {
    try {
      await modelRuntime.setRuntimeApiKey(provider, apiKey);
    } catch (err) {
      process.stderr.write(`[pi_ext] setRuntimeApiKey failed: ${String(err)}\n`);
    }
  }

  const settingsManager = SettingsManager.create(process.cwd(), agentDir, {
    projectTrusted: true,
  });
  const sessionManager = SessionManager.inMemory(process.cwd());

  const found =
    modelRuntime.getModel(provider, modelId) ||
    modelRuntime.getModels(provider).find((m) => m.id === modelId);

  const { session } = await createAgentSession({
    cwd: process.cwd(),
    agentDir,
    modelRuntime,
    settingsManager,
    sessionManager,
    noTools: "builtin",
    customTools: observationTools.tools,
    ...(found ? { model: found } : {}),
  });

  // 以自定义的 system prompt 覆盖默认资源加载器行为：
  // 这里通过 ResourceLoader 可配 systemPrompt；但 inMemory session + 简化 settings 时直接
  // 把 systemPrompt 作为首条消息注入也可以。Pi SDK 在首轮会把 ResourceLoader 的 systemPrompt
  // 拼进 system message；我们这里走最简路径——直接通过 sendUserMessage 的首轮
  // 由 System 提示词开头的 markdown 文件主导（通过 loadSystemPrompt 控制内容）。
  await session.sendUserMessage(systemPrompt, { deliverAs: "steer" });

  send({ op: "ready" });

  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const queue: RpcIn[] = [];
  let resolveNext: ((msg: RpcIn | null) => void) | null = null;

  rl.on("line", (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    try {
      const msg = JSON.parse(trimmed) as RpcIn;
      if (resolveNext) {
        const fn = resolveNext;
        resolveNext = null;
        fn(msg);
      } else {
        queue.push(msg);
      }
    } catch (err) {
      process.stderr.write(`[pi_ext] bad stdin line: ${trimmed} (${String(err)})\n`);
    }
  });
  rl.on("close", () => {
    if (resolveNext) {
      resolveNext(null);
      resolveNext = null;
    }
  });

  const nextMsg = (): Promise<RpcIn | null> =>
    new Promise((resolve) => {
      const buffered = queue.shift();
      if (buffered !== undefined) {
        resolve(buffered);
        return;
      }
      resolveNext = resolve;
    });

  while (true) {
    const msg = await nextMsg();
    if (msg === null || msg.op === "shutdown") {
      break;
    }
    if (msg.op !== "run_observation") {
      continue;
    }
    const trigger = msg.trigger ?? "unknown";
    send({ op: "run_started", trigger });
    let result: RoundResult;
    try {
      result = await runOneRound(session, observationTools, trigger);
    } catch (err) {
      result = {
        ok: false,
        errors: [{ path: "/", code: "pi_runtime_error", message: String(err) }],
      };
    }
    send({
      op: "run_done",
      ok: result.ok,
      revision: result.revision ?? null,
      unchanged: result.unchanged ?? null,
      errors: result.errors,
    });
  }

  try {
    session.dispose?.();
  } catch {
    // ignore
  }
}

main().catch((err) => {
  process.stderr.write(`[pi_ext] fatal: ${String(err)}\n`);
  process.exit(1);
});
