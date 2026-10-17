/** Mirrors backend/app/schemas/schemas.py. Money is always an exact decimal string. */

export type Status = 'high' | 'review' | 'needs_review' | 'not_found'
export type CompletenessStatus = 'complete' | 'partial' | 'needs_review' | 'failed'
export type Engine = 'local' | 'groq' | 'hybrid'

/** A user-facing message: translate `code` with `params`; `text` is the English fallback. */
export interface Message {
  code: string
  params: Record<string, unknown>
  text: string
}

export interface SourceRef {
  page: number
  bbox: number[]
  text: string | null
  anchor: string | null
}

export interface Evidence {
  engine?: string | null
  page?: number | null
  bbox?: number[] | null
  lines?: string[] | null
  excerpt?: string | null
  text_source?: string | null
  verified?: Record<string, boolean> | boolean | null
  confirmed_by?: string | null
}

export interface HeaderField {
  id: string | null
  account: string
  name: string
  label: string
  value: string | null
  normalized: string | null
  value_type: string
  required: boolean
  found: boolean
  confidence: number | null
  status: Status
  validation: 'valid' | 'invalid' | 'not_found' | 'review'
  validation_message: string | null
  validation_code: string | null
  reasons: string[]
  reason_codes: Message[]
  source: SourceRef | null
  alternatives: string[]
  provenance: string | null
  evidence: Evidence | null
  corrected: boolean
  original_value: string | null
}

export interface Column {
  key: string
  label: string
  required: boolean
  found: boolean
  printed_as: string | null
}

export interface Transaction {
  id: string | null
  account: string
  index: number
  page: number
  date: string | null
  value_date: string | null
  date_inherited: boolean
  description: string | null
  reference: string | null
  debit: string | null
  credit: string | null
  amount: string | null
  balance: string | null
  currency: string | null
  direction_inferred: boolean
  balance_check: 'ok' | 'mismatch' | 'unchecked'
  confidence: number | null
  status: 'high' | 'review' | 'needs_review'
  issues: string[]
  issue_codes: Message[]
  reasons: string[]
  reason_codes: Message[]
  bbox: number[] | null
  source_engine: string | null
  provenance: string | null
  evidence: Evidence | null
  corrected: boolean
  deleted: boolean
  original: Record<string, unknown> | null
}

export interface Check {
  key: string
  label: string
  status: 'pass' | 'fail' | 'warn' | 'skipped' | 'info'
  detail: string
  detail_code: string
  params: Record<string, unknown>
}

export interface BalanceRow {
  kind: string
  page: number
  text: string
  amount: string | null
  date: string | null
}

export interface UnresolvedLine {
  index: number
  page: number
  text: string
  reason: string
  amounts: string[]
  bbox: number[] | null
  dismissed: boolean
}

export interface Totals {
  opening_balance: string | null
  opening_source: string | null
  opening_date: string | null
  closing_balance: string | null
  closing_source: string | null
  closing_date: string | null
  currency: string | null
  mode: string
  order: string
  total_debits: string
  total_credits: string
  printed_debits: string | null
  printed_credits: string | null
  net_movement: string
  unresolved_rows: number
}

export interface Account {
  key: string
  label: string | null
  engine: string | null
  currency: string | null
  header: HeaderField[]
  columns: Column[]
  transactions: Transaction[]
  checks: Check[]
  totals: Totals
  balance_rows: BalanceRow[]
  unresolved: UnresolvedLine[]
  duplicates: number[][]
  has_balance_column: boolean
  empty_table_text: string | null
  notes: string[]
  note_codes: Message[]
}

export interface Completeness {
  status: CompletenessStatus
  reasons: Message[]
  metrics: Record<string, unknown>
}

export interface Chunk {
  index: string
  pages: number[]
  lines: number
  status: string
  error_code: string | null
  attempts: number
  input_tokens: number
  output_tokens: number
  reasoning_tokens: number
  latency_ms: number
}

export interface Run {
  id: string
  status: 'queued' | 'processing' | 'completed' | 'failed'
  stage: string | null
  progress_current: number | null
  progress_total: number | null
  engine_requested: string | null
  engine_used: string | null
  fallback_reason: string | null
  model: string | null
  prompt_version: string | null
  schema_version: string | null
  llm_calls: number | null
  llm_input_tokens: number | null
  llm_output_tokens: number | null
  llm_latency_ms: number | null
  pages_processed: number | null
  pages_failed: number | null
  chunks: Chunk[]
  error: Message | null
  created_at: string | null
  started_at: string | null
  completed_at: string | null
}

export interface DocumentInfo {
  id: string
  filename: string
  file_type: string
  page_count: number
  file_size_bytes: number
  created_at: string | null
  ocr_used: boolean
  preview_url: string
}

export interface EngineInfo {
  requested: string | null
  used: string | null
  fallback_reason: string | null
  selected: Record<string, string>
  disagreements: Record<string, unknown>[]
  disagreement_count: number
  model: string | null
}

export interface Summary {
  headline: Message
  detail: Message
  tone: 'ready' | 'review' | 'issue'
  accounts: number
  header_total: number
  header_found: number
  header_missing: string[]
  rows: number
  rows_high: number
  rows_review: number
  rows_needs_review: number
  rows_reconciled: number
  rows_mismatched: number
  rows_corrected: number
  rows_deleted: number
  overall_confidence: number | null
}

export interface Conventions {
  decimal: string
  group: string
  confident: boolean
  date_order: string
}

export interface ExtractionResult {
  document: DocumentInfo
  run: Run | null
  summary: Summary
  completeness: Completeness
  engine: EngineInfo
  accounts: Account[]
  conventions: Conventions
  notice: Message | null
  corrected: boolean
  corrections: number
  revalidated_at: string | null
}

export interface UploadResponse {
  document_id: string
  run_id: string
  status: string
  engine: Engine
  page_count: number
  duplicate_of_running: boolean
}

export interface DocumentListItem {
  id: string
  filename: string
  created_at: string | null
  page_count: number
  file_size_bytes: number
  status: string | null
  stage: string | null
  engine_requested: string | null
  engine_used: string | null
  completeness: CompletenessStatus | null
  rows: number
  overall_confidence: number | null
  corrected: boolean
  error_code: string | null
}

export interface DocumentList {
  items: DocumentListItem[]
  total: number
  limit: number
  offset: number
}

export interface Correction {
  id: string
  entity_type: string
  entity_id: string
  action: string
  attribute: string | null
  old_value: string | null
  new_value: string | null
  user: string
  created_at: string | null
}

export interface EngineAvailability {
  name: Engine
  available: boolean
  external: boolean
  problems: { code: string; message: string }[]
}

export interface AppConfig {
  default_engine: Engine
  engines: EngineAvailability[]
  external_calls_allowed: boolean
  model: string
  prompt_version: string
  max_file_size_bytes: number
  max_pages: number
  allowed_extensions: string[]
  auth_mode: 'none' | 'token'
  retention_days: number
}

export interface TransactionInput {
  date?: string | null
  value_date?: string | null
  description?: string | null
  reference?: string | null
  debit?: string | null
  credit?: string | null
  amount?: string | null
  balance?: string | null
  currency?: string | null
  confirmed?: boolean
  not_duplicate?: boolean
}
