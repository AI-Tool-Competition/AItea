export interface User {
  id: string;
  name: string;
  email: string;
  is_demo: boolean;
}
export interface Mode {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
}
export interface FieldDefinition {
  key: string;
  label: string;
  type: "text" | "number" | "textarea";
  required: boolean;
  min?: number;
  placeholder?: string;
}
export interface TaskDefinition {
  id: string;
  title: string;
  description: string;
  required: boolean;
  fields: FieldDefinition[];
  require_document: boolean;
  require_confirmation: boolean;
  output_template?: string;
}
export interface StageDefinition {
  id: string;
  name: string;
  actor: string;
  description: string;
  tasks: TaskDefinition[];
  analysis?: AgentConfig | null;
}
export interface AgentStep {
  id: string;
  type: "retrieve" | "check" | "guide" | "draft" | "verify";
  label: string;
  enabled: boolean;
  instructions?: string;
}
export interface AgentConfig {
  language: string;
  instructions: string;
  retrieval_limit: number;
  steps: AgentStep[];
}
export interface Template {
  id: string;
  version: number;
  title: string;
  description: string;
  origin: string;
  destination: string;
  is_demo: boolean;
  modes: Mode[];
  stages: StageDefinition[];
  agent: AgentConfig;
}
export interface CaseSummary {
  id: string;
  title: string;
  company: string;
  product: string;
  mode_id: string;
  template_version: number;
  created_at: string;
  updated_at: string;
  completed_tasks: number;
  total_tasks: number;
  current_stage_id: string;
}
export interface Check {
  id: string;
  task_id?: string;
  task_title?: string;
  label: string;
  status: "passed" | "missing" | "conflict";
  reason: string;
  evidence_refs: string[];
}
export interface Verification {
  id: string;
  status: "passed" | "needs_input";
  checks: Check[];
  created_at: string;
  task_revision: number;
}
export interface TaskState extends TaskDefinition {
  status: "pending" | "in_progress" | "needs_input" | "completed";
  needs_review: boolean;
  revision: number;
  values: Record<string, string | number>;
  evidence_ids: string[];
  confirmed: boolean;
  verification: Verification | null;
}
export interface StageState {
  id: string;
  name: string;
  actor: string;
  description: string;
  status: "locked" | "active" | "completed";
  tasks: TaskState[];
}
export interface Fact {
  key: string;
  label: string;
  value: string | number;
  source: string;
  confirmed: boolean;
  updated_at: string;
}
export interface AuditEvent {
  id: string;
  type: string;
  title: string;
  detail: string;
  created_at: string;
}
export interface DocumentMetadata {
  kind: "law" | "guidance" | "case" | "business" | "demo";
  jurisdiction: string;
  language: string;
  published_at: string | null;
  effective_from: string | null;
  effective_to: string | null;
  review_status: "draft" | "published";
}
export interface Document {
  family_id?: string;
  content_hash?: string;
  legal_level?: "primary" | "secondary" | "attachment";
  classifications?: Classification[];
  metadata?: DocumentMetadata;
  metadata_revision?: number;
  reviewed_at?: string | null;
  id: string;
  title: string;
  filename: string;
  role: "primary" | "secondary";
  status: "ready" | "failed" | "stored" | "processing";
  mime_type: string;
  size: number;
  version: number;
  case_id: string | null;
  parent_id: string | null;
  tags: string[];
  created_at: string;
  source_url: string | null;
  error: string | null;
  chunk_count: number;
}
export interface Artifact {
  id: string;
  case_id: string;
  task_id: string;
  title: string;
  content: string;
  version: number;
  confirmed: boolean;
  created_at: string;
  updated_at: string;
}
export interface CaseDetail extends CaseSummary {
  template: Template;
  revision: number;
  stages: StageState[];
  facts: Fact[];
  documents: Document[];
  artifacts: Artifact[];
  events: AuditEvent[];
}
export interface Citation {
  metadata?: DocumentMetadata;
  metadata_revision?: number;
  document_id: string;
  title: string;
  excerpt: string;
  locator: Record<string, unknown>;
  source_url?: string;
  document_version?: number;
}
export interface TraceStep {
  id: string;
  label: string;
  status:
    | "completed"
    | "skipped"
    | "failed"
    | "running"
    | "cached"
    | "cancelled";
  detail: string;
  type?: AgentStep["type"];
  duration_ms?: number;
  input?: string;
  output?: string;
  input_data?: Record<string, unknown>;
  output_data?: Record<string, unknown>;
}
export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  stage_id: string;
  task_id: string | null;
  created_at: string;
  citations: Citation[];
  trace: TraceStep[];
}
export interface MessageInput {
  stage_id: string;
  task_id?: string;
  content: string;
  client_message_id: string;
}
export interface MessageResult {
  messages: Message[];
  case: CaseDetail;
}
export type MessageStreamEvent =
  | { event: "status"; data: { label: string; step_id?: string } }
  | { event: "start"; data: { step_id: string } }
  | { event: "delta"; data: { text: string } };
export interface Capabilities {
  assistant_mode: "demo" | "live";
  retrieval_mode: "lexical" | "hybrid";
  parser: string;
}
export interface Bootstrap {
  user: User;
  templates: Template[];
  cases: CaseSummary[];
  capabilities: Capabilities;
}
export interface Source {
  revision?: number;
  plan?: CollectionPlan;
  latest_run?: CollectionRun | null;
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  interval_hours: number;
  tags: string[];
  last_fetched_at: string | null;
  status: string;
  last_error: string | null;
}
export interface CollectionPlan {
  collection: string;
  level: "primary" | "secondary";
  seed_urls: string[];
  primary_document_ids: string[];
  discover_attachments: boolean;
  max_items: number;
}
export interface Classification {
  collection: string;
  level: "primary" | "secondary";
  tags: string[];
  revision: number;
}
export interface CollectionRun {
  id: string;
  source_id: string;
  status: "queued" | "running" | "completed" | "partial" | "cancelled";
  revision: number;
  created_at: string;
  updated_at: string;
  error: string | null;
  total: number;
  stored: number;
  failed: number;
  pending: number;
  processing: number;
  processed: number;
  processing_failed: number;
}
export interface CollectionItem {
  id: string;
  url: string;
  level: "primary" | "secondary" | "attachment";
  status: "queued" | "fetching" | "stored" | "failed" | "cancelled";
  attempts: number;
  document_id: string | null;
  error: string | null;
  processing_status: string | null;
  processing_error?: string | null;
}
export interface CollectionRunDetail extends ListResult<CollectionItem> {
  run: CollectionRun;
}
export interface LawRelation {
  id: string;
  collection: string;
  relation: "cites" | "related";
  source_document_id: string;
  target_document_id: string;
  locator: string;
  created_at: string;
  source_title: string;
  target_title: string;
  source_version: number;
  target_version: number;
}
export interface ListResult<T> {
  items: T[];
  total: number;
}
export interface ValidationResult {
  valid: boolean;
  errors: { path: string; message: string }[];
  preview: { stages: number; tasks: number };
}
export interface DebugResult {
  output: string;
  trace: TraceStep[];
  checks: Check[];
  citations: Citation[];
  stage_id: string;
  assistant_mode: "demo" | "live";
}
export interface DebugExpectation {
  id: string;
  kind: "contains" | "not_contains" | "check" | "citation" | "manual";
  text?: string | null;
  task_id?: string | null;
  check_id?: string | null;
  status?: Check["status"] | null;
  document_id?: string | null;
}
export interface DebugRequest {
  template: Template;
  stage_id: string;
  input: string;
  task_values: Record<string, Record<string, string | number>>;
  document_ids: string[] | null;
  expectations: DebugExpectation[];
  client_id?: string | null;
  sample_id?: string | null;
  sample_revision?: number | null;
  source_run_id?: string | null;
  start_step_id?: string | null;
  stop_after_step_id?: string | null;
}
export interface DebugDocumentVersion {
  document_id: string;
  version: number;
  metadata_revision: number;
}
export interface DebugSampleSummary {
  id: string;
  name: string;
  stage_id: string;
  revision: number;
  updated_at: string;
}
export interface DebugSample extends DebugSampleSummary {
  template_id: string;
  template: Template;
  input: string;
  task_values: DebugRequest["task_values"];
  document_ids: string[] | null;
  expectations: DebugExpectation[];
  document_versions: DebugDocumentVersion[];
}
export interface DebugSampleInput {
  template: Template;
  name: string;
  stage_id: string;
  input: string;
  task_values: DebugRequest["task_values"];
  document_ids: string[] | null;
  expectations: DebugExpectation[];
  expected_revision: number;
}
export interface DebugEvaluation {
  id: string;
  label: string;
  status: "passed" | "failed" | "manual";
  actual: string;
}
export interface DebugRun extends DebugResult {
  id: string;
  created_at: string;
  status: "completed" | "paused" | "failed" | "cancelled";
  request: DebugRequest;
  sample_id: string | null;
  source_run_id: string | null;
  evaluations: DebugEvaluation[];
  document_versions: DebugDocumentVersion[];
  error?: { code: string; message: string } | null;
}
export interface DebugRunSummary {
  id: string;
  created_at: string;
  status: DebugRun["status"];
  stage_id: string;
  sample_id: string | null;
  evaluation_status: "passed" | "failed" | "manual" | "none";
}
export type DebugStreamEvent =
  | { event: "run"; data: { id: string } }
  | { event: "step"; data: TraceStep }
  | { event: "delta"; data: { text: string; step_id?: string } }
  | { event: "status"; data: { label: string; step_id?: string } }
  | { event: "done"; data: DebugRun }
  | { event: "error"; data: { code: string; message: string } };
export interface TemplateDraft {
  template: Template | null;
  revision: number;
  updated_at: string | null;
}
export interface TemplateDraftSummary {
  id: string;
  title: string;
  revision: number;
  updated_at: string;
}
export interface StageAnalysis extends DebugResult {
  id: string;
  case_revision: number;
  status: "completed" | "failed";
  created_at: string;
}
export interface DocumentPart {
  element_key?: string;
  id?: string;
  kind?: string;
  content_text?: string;
  text?: string;
  section_path?: string[];
  locator?: Record<string, unknown>;
}
export interface DocumentDetail {
  document: Document;
  elements: DocumentPart[];
  chunks: DocumentPart[];
  element_total: number;
  chunk_total: number;
  warnings: string[];
  parser?: string | null;
}
