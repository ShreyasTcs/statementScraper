import type {
  AppConfig,
  Correction,
  DocumentList,
  Engine,
  ExtractionResult,
  Run,
  TransactionInput,
  UploadResponse,
} from '../types'

const BASE = '/api/v1'
const TOKEN_KEY = 'stmt.token'

/** An API error: a stable code (translated by the UI), params, and the server's English text. */
export class ApiRequestError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly params: Record<string, unknown> = {},
  ) {
    super(message)
  }
}

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* storage unavailable: the token lives only for this page */
  }
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken()
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra
}

async function toError(response: Response): Promise<ApiRequestError> {
  try {
    const body = await response.json()
    const detail = body?.detail
    if (detail && typeof detail === 'object' && typeof detail.code === 'string') {
      return new ApiRequestError(response.status, detail.code, detail.message ?? '', detail.params ?? {})
    }
  } catch {
    /* not JSON */
  }
  return new ApiRequestError(response.status, response.status === 401 ? 'unauthorized' : 'server_error', response.statusText)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, { ...init, headers: headers((init.headers as Record<string, string>) ?? {}) })
  } catch {
    throw new ApiRequestError(0, 'network', 'We could not reach the extraction service.')
  }
  if (!response.ok) throw await toError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

function json(method: string, body: unknown): RequestInit {
  return { method, body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } }
}

async function blob(path: string): Promise<Blob> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, { headers: headers() })
  } catch {
    throw new ApiRequestError(0, 'network', 'We could not reach the extraction service.')
  }
  if (!response.ok) throw await toError(response)
  return response.blob()
}

export function saveBlob(content: Blob, filename: string): void {
  const url = URL.createObjectURL(content)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

const doc = (id: string) => `/documents/${encodeURIComponent(id)}`

export const api = {
  config: () => request<AppConfig>('/config'),

  upload(file: File, engine?: Engine): Promise<UploadResponse> {
    const form = new FormData()
    form.append('file', file)
    if (engine) form.append('engine', engine)
    return request<UploadResponse>('/documents', { method: 'POST', body: form })
  },

  status: (id: string) => request<{ document_id: string; run: Run }>(`${doc(id)}/status`),
  result: (id: string) => request<ExtractionResult>(`${doc(id)}/result`),
  list: (params: { limit?: number; offset?: number; q?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.limit) query.set('limit', String(params.limit))
    if (params.offset) query.set('offset', String(params.offset))
    if (params.q) query.set('q', params.q)
    return request<DocumentList>(`/documents?${query.toString()}`)
  },
  remove: (id: string) => request<void>(doc(id), { method: 'DELETE' }),
  reprocess: (id: string, engine: Engine) => request<UploadResponse>(`${doc(id)}/reprocess`, json('POST', { engine })),
  corrections: (id: string) => request<Correction[]>(`${doc(id)}/corrections`),
  revalidate: (id: string) => request<ExtractionResult>(`${doc(id)}/revalidate`, { method: 'POST' }),

  updateField: (id: string, fieldId: string, body: { value?: string | null; confirm?: boolean }) =>
    request<ExtractionResult>(`${doc(id)}/fields/${encodeURIComponent(fieldId)}`, json('PATCH', body)),
  updateTransaction: (id: string, txnId: string, body: TransactionInput) =>
    request<ExtractionResult>(`${doc(id)}/transactions/${encodeURIComponent(txnId)}`, json('PATCH', body)),
  createTransaction: (id: string, body: TransactionInput & { account: string; after_index: number | null; page?: number }) =>
    request<ExtractionResult>(`${doc(id)}/transactions`, json('POST', body)),
  deleteTransaction: (id: string, txnId: string) =>
    request<ExtractionResult>(`${doc(id)}/transactions/${encodeURIComponent(txnId)}`, { method: 'DELETE' }),
  restoreTransaction: (id: string, txnId: string) =>
    request<ExtractionResult>(`${doc(id)}/transactions/${encodeURIComponent(txnId)}/restore`, { method: 'POST' }),
  dismissUnresolved: (id: string, account: string, index: number, dismissed: boolean) =>
    request<ExtractionResult>(
      `${doc(id)}/unresolved/${encodeURIComponent(account)}/${index}/dismiss?dismissed=${dismissed}`,
      { method: 'POST' },
    ),

  downloadCsv: async (id: string, filename: string) => saveBlob(await blob(`${doc(id)}/transactions.csv`), filename),
  downloadJson: async (id: string, filename: string) => saveBlob(await blob(`${doc(id)}/result.json`), filename),
  /** The original upload as an object URL (works with token auth, unlike a plain link). */
  previewUrl: async (id: string) => URL.createObjectURL(await blob(`${doc(id)}/preview`)),
}
