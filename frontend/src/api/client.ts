/** 统一 API 客户端：解析后端标准错误结构（Plan §4.5）。 */

/** 与后端 `logging_conf.REQUEST_ID_HEADER` 一致，链路：浏览器 → Nginx → 后端日志。 */
export const REQUEST_ID_HEADER = 'X-Request-ID'

/**
 * 生成请求 ID。
 *
 * `crypto.randomUUID` 只在**安全上下文**（https / localhost）可用；NAS 内网走
 * `http://192.168.2.2` 是**非安全上下文**，那里 `crypto.randomUUID` 是
 * `undefined`。所以先探测再降级到 `getRandomValues`（不受此限制）。
 * 最后再降级到 `Math.random`：只为排障关联，不承担安全职责。
 */
function newRequestId(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  if (c && typeof c.getRandomValues === 'function') {
    const buf = new Uint8Array(16)
    c.getRandomValues(buf)
    return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('')
  }
  return Math.random().toString(16).slice(2) + Date.now().toString(16)
}

export class ApiError extends Error {
  status: number
  code: string
  detail: Record<string, unknown>
  /** 后端（或 Nginx）回写的请求 ID——报障时把这一个字符串给出来即可对上日志。 */
  requestId: string | null

  constructor(
    status: number,
    code: string,
    message: string,
    detail: Record<string, unknown>,
    requestId: string | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
    this.requestId = requestId
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const requestId = newRequestId()
  const res = await fetch(path, {
    ...init,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      [REQUEST_ID_HEADER]: requestId,
      ...(init?.headers ?? {}),
    },
  })
  // 优先用服务端回写的（Nginx 可能替换为它自己的），没有则用本地发出去的那个。
  const echoed = res.headers.get(REQUEST_ID_HEADER) ?? requestId
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
    throw new ApiError(res.status, code, message, detail, echoed)
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
