export interface RpcIn {
  op: "run_observation" | "shutdown";
  observer_session_id?: string;
  tools?: string[];
  trigger?: string;
}

export interface RpcOut {
  op: "ready" | "run_started" | "run_done" | "log";
  observer_session_id?: string;
  tools?: string[];
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
