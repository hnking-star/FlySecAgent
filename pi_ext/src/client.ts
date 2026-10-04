/**
 * HTTP client for FlySecAgent's `/memory/*` routes.
 *
 * Does not throw on 4xx/5xx — returns the body so the model can read the
 * structured error list (code / path / message) and self-correct.
 */

export interface CuratorResponse {
  ok?: boolean;
  revision?: string | null;
  published?: boolean;
  unchanged?: boolean | null;
  errors?: unknown;
  warnings?: unknown;
  [key: string]: unknown;
}

export class CuratorClient {
  constructor(
    private readonly api: string,
    private readonly token: string,
  ) {}

  read(args: unknown): Promise<CuratorResponse> {
    return this._post("/memory/read", args);
  }

  commit(args: unknown): Promise<CuratorResponse> {
    return this._post("/memory/commit", args);
  }

  private async _post(path: string, body: unknown): Promise<CuratorResponse> {
    const res = await fetch(`${this.api}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-FlySec-Token": this.token,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10_000),
    });
    // Fall through on non-2xx; service returns a JSON error body (ok:false ...).
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      data = { ok: false, errors: [{ path: "/", code: "non_json_response",
        message: `HTTP ${res.status} from ${path}` }] };
    }
    if (res.status === 404 && data && typeof data === "object" && !("code" in data)) {
      data = { ok: false, code: "protocol_unavailable", errors: [{ path: "/", code: "protocol_unavailable", message: "Memory v2 service is not available at this endpoint" }] };
    }
    return (data ?? {}) as CuratorResponse;
  }
}
