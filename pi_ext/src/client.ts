/**
 * HTTP client for FlySecAgent's `/observer/*` routes.
 *
 * Does not throw on 4xx/5xx — returns the body so the model can read the
 * structured error list (code / path / message) and self-correct.
 */

export interface ObserverResponse {
  ok?: boolean;
  revision?: string | null;
  unchanged?: boolean | null;
  errors?: unknown;
  warnings?: unknown;
  [key: string]: unknown;
}

export class ObserverClient {
  constructor(
    private readonly api: string,
    private readonly token: string,
  ) {}

  context(args: unknown): Promise<ObserverResponse> {
    return this._post("/observer/context", args);
  }

  submit(args: unknown): Promise<ObserverResponse> {
    return this._post("/observer/submit", args);
  }

  private async _post(path: string, body: unknown): Promise<ObserverResponse> {
    const res = await fetch(`${this.api}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-FlySec-Token": this.token,
      },
      body: JSON.stringify(body),
    });
    // Fall through on non-2xx; service returns a JSON error body (ok:false ...).
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      data = { ok: false, errors: [{ path: "/", code: "non_json_response",
        message: `HTTP ${res.status} from ${path}` }] };
    }
    return (data ?? {}) as ObserverResponse;
  }
}
