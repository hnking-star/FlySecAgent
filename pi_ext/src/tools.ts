import { defineTool } from "@earendil-works/pi-coding-agent";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import type { ObserverClient, ObserverResponse } from "./client.js";

const ContextSchema = Type.Object(
  {
    mode: Type.Union(
      [
        Type.Literal("summary"),
        Type.Literal("window_records"),
        Type.Literal("record_detail"),
        Type.Literal("blackboard"),
        Type.Literal("history_record"),
      ],
      { default: "summary" },
    ),
    record_id: Type.Optional(Type.Integer({ minimum: 1 })),
    offset: Type.Optional(Type.Integer({ minimum: 0 })),
    length: Type.Optional(Type.Integer({ minimum: 1 })),
    assessment_ids: Type.Optional(
      Type.Array(Type.String(), { maxItems: 16 }),
    ),
  },
  { additionalProperties: false },
);

const AttemptSchema = Type.Object(
  {
    id: Type.String(),
    action: Type.String(),
    result: Type.String(),
    assessment: Type.Optional(Type.Union([Type.String(), Type.Null()])),
    evidenceRefs: Type.Array(Type.String(), { minItems: 1, maxItems: 16 }),
  },
  { additionalProperties: false },
);

const AssessmentSchema = Type.Object(
  {
    id: Type.String(),
    subject: Type.String(),
    status: Type.Union([
      Type.Literal("inferred-open"),
      Type.Literal("tried-hit"),
      Type.Literal("tried-miss"),
      Type.Literal("scan-class"),
    ]),
    role: Type.Optional(
      Type.Union([
        Type.Literal("direction"),
        Type.Literal("endpoint"),
        Type.Literal("path"),
      ]),
    ),
    conclusion: Type.String(),
    basis: Type.String(),
    uncertainty: Type.Union([Type.String(), Type.Null()]),
    evidenceRefs: Type.Array(Type.String(), { minItems: 1, maxItems: 16 }),
    attempts: Type.Optional(Type.Array(AttemptSchema, { maxItems: 20 })),
    dependsOn: Type.Optional(Type.Array(Type.String(), { maxItems: 16 })),
    apiIds: Type.Optional(Type.Array(Type.String(), { maxItems: 32 })),
  },
  { additionalProperties: false },
);

const ApiTestSchema = Type.Object(
  {
    id: Type.String(),
    action: Type.String(),
    result: Type.String(),
    record_ids: Type.Array(Type.Integer({ minimum: 1 }), { minItems: 1, maxItems: 16 }),
  },
  { additionalProperties: false },
);

const ApiParamSchema = Type.Object(
  {
    name: Type.String(),
    description: Type.Optional(Type.Union([Type.String(), Type.Null()])),
  },
  { additionalProperties: false },
);

const ApiEntrySchema = Type.Object(
  {
    id: Type.String(),
    endpoint: Type.String(),
    purpose: Type.String(),
    parameters: Type.Optional(Type.Array(ApiParamSchema, { maxItems: 32 })),
    tests: Type.Optional(Type.Array(ApiTestSchema, { maxItems: 32 })),
  },
  { additionalProperties: false },
);

const GuidanceSchema = Type.Object(
  {
    hypothesis: Type.Optional(Type.Union([Type.String(), Type.Null()])),
    lock: Type.Optional(Type.Union([Type.String(), Type.Null()])),
    angleIds: Type.Optional(Type.Array(Type.String(), { maxItems: 4 })),
    confirmedIds: Type.Optional(Type.Array(Type.String(), { maxItems: 8 })),
    tension: Type.Optional(Type.Array(Type.String(), { maxItems: 2 })),
  },
  { additionalProperties: false },
);

const SubmitSchema = Type.Object(
  {
    baseRevision: Type.Union([Type.String(), Type.Null()]),
    upserts: Type.Optional(Type.Array(AssessmentSchema, { maxItems: 30 })),
    retireIds: Type.Optional(Type.Array(Type.String(), { maxItems: 32 })),
    apis: Type.Optional(Type.Array(ApiEntrySchema, { maxItems: 32 })),
    guidance: Type.Optional(Type.Union([GuidanceSchema, Type.Null()])),
  },
  { additionalProperties: false },
);

/** Wrap a response object as a tool text result. */
function textResult(payload: ObserverResponse) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(payload) }],
    details: payload,
  };
}

export interface ObservationTools {
  /** All registered tools passed to Pi SDK. */
  tools: ToolDefinition[];
  /** Latest submit response captured by the wrapper (null when not yet submitted). */
  lastSubmit: () => ObserverResponse | null;
  /** Reset the captured state before a new round. */
  reset: () => void;
}

export function createObservationTools(client: ObserverClient): ObservationTools {
  let lastSubmit: ObserverResponse | null = null;

  const context = defineTool({
    name: "observation_context",
    label: "Observation context",
    description:
      "Read FlySecAgent observation context. mode=summary (default) gives project / window / records_overview / blackboard revision / assessments / API ledger summaries / guidance / last_errors. Other modes: window_records, record_detail(record_id, offset?, length?), blackboard, history_record(record_id).",
    parameters: ContextSchema,
    execute: async (_id, args) => {
      const result = await client.context(args);
      return textResult(result);
    },
  });

  const submit = defineTool({
    name: "observation_submit",
    label: "Submit observation",
    description:
      "Submit a flat observation increment. Fields: baseRevision (null first time, must equal the last observation_context revision thereafter); upserts[] of {id, subject, status, role?, conclusion, basis, uncertainty, evidenceRefs, attempts?, dependsOn?, apiIds?}; retireIds[]; apis[] of {id, endpoint, purpose, parameters?, tests?}. apiIds must reference API IDs already on the blackboard or included in the same submission. API tests append by stable test.id. guidance{hypothesis?, lock?, angleIds?, confirmedIds?, tension?}. On ok:false, read errors[].code and retry in the same window; the round only ends after ok:true.",
    parameters: SubmitSchema,
    executionMode: "sequential",
    execute: async (_id, args) => {
      const result = await client.submit(args);
      lastSubmit = result;
      return textResult(result);
    },
  });

  return {
    tools: [context, submit],
    lastSubmit: () => lastSubmit,
    reset: () => {
      lastSubmit = null;
    },
  };
}
