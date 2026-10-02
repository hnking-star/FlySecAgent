export interface RpcIn {
  op: "run_observation" | "shutdown";
  trigger?: string;
}

export interface RpcOut {
  op: "ready" | "run_started" | "run_done" | "log";
  trigger?: string;
  ok?: boolean;
  revision?: string | null;
  unchanged?: boolean | null;
  errors?: unknown;
  level?: "info" | "warn" | "error";
  message?: string;
}

export interface SubmitError {
  path?: string;
  code?: string;
  message?: string;
}
