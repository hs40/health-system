/** 统一 API 客户端：解析后端标准错误结构（Plan §4.5）。 */

export class ApiError extends Error {
  status: number
  code: string
  detail: Record<string, unknown>

  constructor(status: number, code: string, message: string, detail: Record<string, unknown>) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    let code = 'E_UNKNOWN'
    let message = res.statusText || `HTTP ${res.status}`
    let detail: Record<string, unknown> = {}
    try {
      const body = await res.json()
      if (body?.error) {
        code = body.error.code ?? code
        message = body.error.message ?? message
        detail = body.error.detail ?? {}
      }
    } catch {
      /* 非 JSON 响应，保留默认 */
    }
    throw new ApiError(res.status, code, message, detail)
  }
  return (await res.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown, idempotencyKey?: string) =>
    request<T>(path, {
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
      headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
    }),
  put: <T>(path: string, body?: unknown, idempotencyKey?: string) =>
    request<T>(path, {
      method: 'PUT',
      body: body ? JSON.stringify(body) : undefined,
      headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
    }),
  del: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'DELETE',
      body: body ? JSON.stringify(body) : undefined,
    }),
}
