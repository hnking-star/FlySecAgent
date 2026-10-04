import { readFileSync } from "node:fs";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { CuratorClient, type CuratorResponse } from "./client.js";

export interface CuratorTools {
  tools: ToolDefinition[];
  lastSubmit: () => CuratorResponse | null;
  fatal: () => boolean;
  reset: () => void;
}

export function createCuratorTools(client: CuratorClient): CuratorTools {
  const schemas = JSON.parse(readFileSync(new URL("./resources/curator-tools.schema.json", import.meta.url), "utf8"));
  let receipt: CuratorResponse | null = null;
  let fatal = false;
  const wrap = (result: CuratorResponse) => {
    const code = result.code || (Array.isArray(result.errors) ? result.errors[0]?.code : undefined);
    if (["curator_paused", "observer_paused", "observation_disabled", "session_not_found", "unauthorized", "protocol_retired", "protocol_unavailable", "invalid_memory", "unsupported_schema"].includes(String(code))) fatal = true;
    return { content: [{ type: "text" as const, text: JSON.stringify(result) }], details: result };
  };
  const read = defineTool({
    name: "curator_read", label: "Curator · Read evidence",
    description: "Read this session's evidence memory. summary returns target, objective, a fixed window, current revision, topics, recent observations/tests, API metadata and unanswered questions. records paginates the fixed window using after_id/limit; record returns complete UTF-8 segments (record_id/offset/length) from this session, including history; state returns the complete memory. Do not treat archived legacy_summary notes as verified observations.",
    parameters: Type.Unsafe(schemas.curator_read),
    execute: async (_id, args) => wrap(await client.read(args)),
  });
  const commit = defineTool({
    name: "curator_commit", label: "Curator · Commit memory",
    description: "Commit protocol=2 deltas using the revision from curator_read: topics, facts, tests, apis and questions. Facts/test records append by stable ID; corrections use a NEW fact ID and supersedes. execution describes the tool (completed/error/denied/interrupted/unknown); outcome describes only this test (supports/contradicts/inconclusive/not_evaluated). Failed/incomplete execution must use not_evaluated. APIs mean business/data endpoints, not static JS/CSS/images. Source inspection belongs in facts/tests; tests without an actual API attempt may have empty api_ids. APIs store endpoint/purpose/parameters/evidence_ids; tests link API IDs and are never duplicated inside API entries. origin_fact_ids links a topic to the observations that motivated it. Evidence IDs must be real receipts in this session. A resolved question needs a resolution and evidence. If new records truly add no memory, supply unchanged_reason rather than silently skipping them. The server validates and returns errors for correction; only ok:true is an accepted receipt.",
    parameters: Type.Unsafe(schemas.curator_commit), executionMode: "sequential",
    execute: async (_id, args) => { const result = await client.commit(args); receipt = result; return wrap(result); },
  });
  return { tools: [read, commit], lastSubmit: () => receipt, fatal: () => fatal,
           reset: () => { receipt = null; fatal = false; } };
}
