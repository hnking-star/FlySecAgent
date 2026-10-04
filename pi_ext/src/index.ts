import { mkdirSync, writeFileSync, existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { createHash } from "node:crypto";
import { createInterface } from "node:readline";
import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import { CuratorClient } from "./client.js";
import { runOneRound, type RoundResult } from "./loop.js";
import { loadSystemPrompt } from "./system-prompt.js";
import { createCuratorTools } from "./tools.js";
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

  const provider = (process.env.FLYSEC_PI_PROVIDER || "deepseek").trim();
  const modelId = (process.env.FLYSEC_PI_MODEL || "deepseek-flash").trim();
  const dataDir = resolve(process.env.FLYSEC_DATA_DIR || "data");
  const keyFile = process.env.FLYSEC_PI_API_KEY_FILE || join(dataDir, ".deepseek-key");
  const apiKey = (process.env.FLYSEC_PI_API_KEY || (existsSync(keyFile) ? readFileSync(keyFile, "utf8") : "")).trim();
  if (!apiKey) throw new Error("Configure FLYSEC_PI_API_KEY or the private data/.deepseek-key file");
  const baseUrl = (process.env.FLYSEC_PI_BASE_URL || "https://api.deepseek.com/anthropic").trim();
  const agentDir =
    process.env.FLYSEC_PI_AGENT_DIR?.trim() ||
    join(resolve(process.env.FLYSEC_DATA_DIR || "data"), "pi", "curator-v2", createHash("sha256").update(sessionId).digest("hex"), ".pi");

  process.env.PI_OFFLINE = process.env.PI_OFFLINE || "1";
  process.env.PI_CODING_AGENT_DIR = agentDir;
  if (baseUrl) {
    process.env.ANTHROPIC_BASE_URL = baseUrl;
    process.env.OPENAI_BASE_URL = baseUrl;
  }
  if (apiKey) {
    process.env.ANTHROPIC_API_KEY = apiKey;
    process.env.ANTHROPIC_AUTH_TOKEN = apiKey;
    process.env.OPENAI_API_KEY = apiKey;
  }

  mkdirSync(agentDir, { recursive: true });
  if (baseUrl) {
    seedModelsDoc(agentDir, provider, modelId, baseUrl);
  }

  const client = new CuratorClient(api, token);
  const curatorTools = createCuratorTools(client);
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

  const workspace = join(agentDir, "workspace");
  mkdirSync(workspace, { recursive: true });
  const settingsManager = SettingsManager.create(workspace, agentDir, { projectTrusted: true });
  const resourceLoader = new DefaultResourceLoader({
    cwd: workspace, agentDir, settingsManager, systemPrompt,
    noContextFiles: true, noExtensions: true, noSkills: true,
    noPromptTemplates: true, noThemes: true,
  });
  await resourceLoader.reload();
  const sessionManager = SessionManager.continueRecent(workspace, join(agentDir, "sessions"));
  const found = modelRuntime.getModel(provider, modelId);
  if (!found) throw new Error(`Configured Pi model unavailable: ${provider}/${modelId}`);
  const { session } = await createAgentSession({
    cwd: workspace, agentDir, modelRuntime, settingsManager, sessionManager, resourceLoader,
    tools: ["curator_read", "curator_commit"],
    customTools: curatorTools.tools, model: found,
  });
  const activeTools = session.getActiveToolNames().sort();
  if (activeTools.join(",") !== "curator_commit,curator_read") {
    throw new Error("Unexpected active Curator tools");
  }
  // Creating a session must not invoke the model or consume a record window.
  send({ op: "ready", curator_session_id: session.sessionId, tools: activeTools });

  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const queue: RpcIn[] = [];
  let closed = false;
  let stopping = false;
  let resolveNext: ((msg: RpcIn | null) => void) | null = null;

  rl.on("line", (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    try {
      const msg = JSON.parse(trimmed) as RpcIn;
      if (msg.op === "shutdown") { stopping = true; void session.abort(); }
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
    closed = true; stopping = true; void session.abort();
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
      if (closed) { resolve(null); return; }
      resolveNext = resolve;
    });

  while (true) {
    const msg = await nextMsg();
    if (stopping || msg === null || msg.op === "shutdown") {
      break;
    }
    if (msg.op !== "run_curation" && msg.op !== "run_observation") {
      continue;
    }
    const trigger = msg.trigger ?? "unknown";
    send({ op: "run_started", trigger });
    let result: RoundResult;
    try {
      result = await runOneRound(session, curatorTools, trigger, { shouldStop: () => stopping });
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
    session.dispose();
    rl.close();
  } catch {
    // ignore
  }
}

main().catch((err) => {
  process.stderr.write(`[pi_ext] fatal: ${String(err)}\n`);
  process.exit(1);
});
