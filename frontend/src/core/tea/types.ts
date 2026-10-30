export interface User {
  id: string;
  name: string;
  email: string | null;
  is_demo: boolean;
  can_login: boolean;
  is_admin: boolean;
}
export type ApiModelProvider = "agent" | "embedding" | "rerank";
export interface ApiProviderKeySettings {
  api_key_configured: boolean;
}
export interface ApiModelSettings extends ApiProviderKeySettings {
  base_url: string;
  model: string;
}
export interface ApiSettings {
  revision: string;
  agent: ApiModelSettings;
  embedding: ApiModelSettings;
  rerank: ApiModelSettings;
  mineru: ApiProviderKeySettings;
  agent_inherits_api_key: boolean;
  embedding_inherits_api_key: boolean;
  environment_overrides: string[];
  restart_required: boolean;
}
export interface ApiProviderKeyPatch {
  api_key?: string;
  clear_api_key?: boolean;
}
export interface ApiModelPatch extends ApiProviderKeyPatch {
  base_url?: string;
  model?: string;
}
export interface ApiSettingsPatch {
  expected_revision: string;
  agent?: ApiModelPatch;
  embedding?: ApiModelPatch;
  rerank?: ApiModelPatch;
  mineru?: ApiProviderKeyPatch;
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
  type: "text" | "number" | "textarea" | "select";
  required: boolean;
  min?: number;
  placeholder?: string;
  options?: string[] | null;
  accepted_values?: string[] | null;
}
export interface TaskDefinition {
  id: string;
  title: string;
  description: string;
  required: boolean;
  blocks_next_stage?: boolean;
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
  legal?: LegalPolicy | null;
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
  field_sources?: Record<
    string,
    {
      kind: "user_statement";
      quote: string;
      source_message_id: string;
      agent_run_id: string | null;
      updated_at: string;
    }
  >;
  evidence_ids: string[];
  confirmed: boolean;
  verification: Verification | null;
}
export interface StageState {
  id: string;
  name: string;
  actor: string;
  description: string;
  status: "locked" | "active" | "follow_up" | "completed";
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
  processing_detail?: {
    state:
      | "running"
      | "queued"
      | "ocr_disabled"
      | "needs_review"
      | "failed"
      | "complete"
      | "stored"
      | "incomplete";
    phase: string | null;
    retry_after_seconds: number | null;
  };
  source_scope?: "personal" | "system";
  country?: string | null;
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
  source_scope?: "personal" | "system";
  id?: string;
  chunk_id?: string;
  chunk_hash?: string;
  metadata?: Partial<DocumentMetadata> & {
    evidence_store?: string;
    source_type?: string;
    content_role?: string;
  };
  metadata_revision?: number;
  document_id?: string;
  title: string;
  excerpt: string;
  locator?: Record<string, unknown>;
  source_url?: string;
  document_version?: number;
}
export interface ToolOutputPreview {
  /** Actual output: at most 4,000 characters, or 60,000 for provider thinking. */
  text: string;
  items: { label: string; text: string; status?: string; url?: string }[];
  notice: string;
  truncated: boolean;
}
export interface ToolCall {
  id: string;
  name:
    | "update_business_card"
    | "search_documents"
    | "search_legal_evidence"
    | "lookup_chinese_law"
    | "read_law"
    | "get_law_versions"
    | "check_materials"
    | "generate_guidance"
    | "guide_business"
    | "analyze_legal"
    | "draft_document"
    | "get_handoff"
    | "discover_tools"
    | "tool_search"
    | "load_skill"
    | "list_documents"
    | "read_document"
    | "inspect_image"
    | "convert_long_image_to_pdf"
    | "inspect_document_file"
    | "create_document_file"
    | "read_contract_template"
    | "edit_document_file"
    | "web_search"
    | "recall_memory"
    | "remember_memory"
    | "update_plan"
    | "update_task_brief"
    | "agent_turn"
    | "compact_context"
    | "read_tool_result";
  label: string;
  position: number;
  status: "running" | "retrying" | "completed" | "failed" | "cancelled";
  attempt: number;
  max_attempts: number;
  detail: string;
  duration_ms: number;
  error_code?: string;
  output_preview?: ToolOutputPreview | null;
}
export interface TraceStep {
  delivery_status?:
    | "answered"
    | "partially_answered"
    | "needs_user_input"
    | "execution_failed"
    | "unsupported_jurisdiction";
  run_id?: string;
  sequence?: number;
  tools?: ToolCall[];
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
  conversation_id?: string | null;
  guide_result?: LegalResult | null;
}
export type ChatSkill =
  | "business"
  | "export_planning"
  | "legal_lookup"
  | "contract_review"
  | "litigation_analysis"
  | "long_image_to_pdf"
  | "document_operations"
  | "china-lawyer-analyst"
  | "chinese-law-verifier"
  | "contract-review"
  | "legal-writing"
  | "litigation-analysis"
  | "long-image-to-pdf"
  | "document-operations"
  | "legal-research"
  | "foreign-law-research"
  | "proposition-checking"
  | "adversarial-qc"
  | "sgcite"
  | "bart-statutory-reference-checker"
  | "document-fill"
  | "coquill"
  | "legal-translation"
  | "contract-qa"
  | "redlines"
  | "comms-improver"
  | "guidance"
  | "materials"
  | "legal"
  | "draft"
  | "handoff"
  | "research"
  | "vision";
export interface NodeConversation {
  id: string;
  case_id: string;
  stage_id: string;
  title: string;
  kind: "guide" | "chat";
  created_at: string;
  updated_at: string;
}
export interface MessageInput {
  stage_id: string;
  task_id?: string;
  conversation_id?: string;
  mode?: "auto" | "legal";
  skills?: ChatSkill[];
  content: string;
  client_message_id: string;
}
export interface MessageResult {
  messages: Message[];
  case: CaseDetail;
}
export interface ToolOutputDelta {
  step_id: string;
  run_id: string;
  tool_id: string;
  sequence: number;
  text: string;
}
export type MessageStreamEvent =
  | { event: "status"; data: { label: string; step_id?: string } }
  | { event: "start"; data: { step_id: string; append?: boolean } }
  | { event: "delta"; data: { text: string } }
  | { event: "tool_delta"; data: ToolOutputDelta }
  | { event: "step"; data: TraceStep };
export interface Capabilities {
  assistant_mode: "demo" | "live";
  retrieval_mode:
    | "lexical"
    | "hybrid"
    | "ragflow"
    | "local_pack"
    | "local_pack_vector"
    | "official_web"
    | "unavailable";
  parser: string;
}
export interface SearchResult {
  items: Citation[];
  mode: Capabilities["retrieval_mode"];
  reranked: boolean;
  warning?: string;
}
export interface Bootstrap {
  user: User;
  templates: Template[];
  cases: CaseSummary[];
  capabilities: Capabilities;
  admin_setup_required: boolean;
}
export interface ManagedUser {
  id: string;
  name: string;
  email: string;
  created_at: string;
  can_login: boolean;
  is_admin: boolean;
  revision: number;
}
export interface ManagedUsersPage {
  items: ManagedUser[];
  total: number;
}
export interface Source {
  checklist?: {
    plan_id: string;
    item_id: string;
    country: string;
    version: number;
  };
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
  level: "primary" | "secondary" | "attachment";
  seed_urls: string[];
  primary_document_ids: string[];
  discover_attachments: boolean;
  max_items: number;
  max_original_bytes?: number;
  download_timeout_seconds?: number;
}
export interface LibraryCountry {
  country: string;
}
export interface CollectionBlueprintSummary {
  id: string;
  version: number;
  country: string;
  title: string;
  description: string;
  source_document: { filename: string; date: string; sha256: string };
  item_count: number;
  chapter_count: number;
}
export interface ChecklistEntry {
  url: string;
  purpose: "catalog" | "detail" | "document" | "reference";
  note: string;
}
export interface ChecklistItem {
  id: string;
  chapter_id: string;
  title: string;
  kind: string;
  conditional: boolean;
  trigger: string;
  priority: number;
  entries: ChecklistEntry[];
  tags: string[];
  scope_include: string[];
  scope_exclude: string[];
  steps: string[];
  fields: string[];
  version_rules: string[];
  relation_rules: string[];
  acceptance: string[];
  verbatim_requirements: string;
  execution: "discovery" | "dataset_review";
  adapter: string;
  limits: Record<string, unknown>;
}
export interface CollectionBlueprint extends CollectionBlueprintSummary {
  principles: string[];
  chapters: { id: string; title: string; notes: string[] }[];
  channels: {
    id?: string;
    title?: string;
    name?: string;
    questions: string[] | string;
    item_ids?: string[];
    sources?: string;
  }[];
  limits: Record<string, unknown>;
  items: ChecklistItem[];
}
export interface ChecklistItemState {
  item_id: string;
  source_id: string | null;
  latest_run: CollectionRun | null;
  counts: {
    pages: number;
    remaining_pages?: number;
    candidates: number;
    pending: number;
    stored: number;
    parsed: number;
    reviewed: number;
    excluded: number;
  };
  coverage: "not_started" | "in_progress" | "needs_review" | "blocked";
  last_error: string | null;
}
export interface CollectionBlueprintDetail {
  plan: CollectionBlueprint;
  items: ChecklistItemState[];
}
export interface ChecklistCandidate {
  id: string;
  url: string;
  title: string;
  discovered_from: string;
  status: "pending" | "queued" | "stored" | "excluded";
  document_id: string | null;
  revision: number;
  note: string;
  created_at: string;
  source_record: {
    provider:
      | "setneg"
      | "bkpm"
      | "rcepsec"
      | "ipos"
      | "acra"
      | "wto"
      | "sg_official_html"
      | "mkri"
      | "jdih_kemenkeu";
    interpretation: "unreviewed_source_values";
    fields: Record<string, string | number | null>;
    structure?: {
      unit_type: string;
      number: string | null;
      parent_titles: string[];
      file_name: string;
      anchor_class: string;
      link_mechanism: string;
      related_links?: { label: string; title: string; url: string }[];
    };
    file?: Record<string, string | number>;
    warnings?: string[];
    evidence: { url: string; sha256: string };
  } | null;
}
export interface Classification {
  collection: string;
  level: "primary" | "secondary";
  tags: string[];
  revision: number;
}
export interface CollectionRun {
  kind?: "discovery" | "collection";
  pages?: number;
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
  purpose?: "catalog" | "detail" | "document";
  id: string;
  url: string;
  level: "primary" | "secondary" | "attachment";
  status:
    | "queued"
    | "fetching"
    | "discovered"
    | "stored"
    | "failed"
    | "cancelled";
  attempts: number;
  document_id: string | null;
  error: string | null;
  processing_status: string | null;
  processing_error?: string | null;
  downloaded_bytes?: number;
  download_total_bytes?: number;
  resumable?: boolean;
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
  country?: string | null;
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
  country?: string | null;
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
  case_id: string;
  legal_analysis_id: string;
  legal_revision: number;
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
  preview?: {
    summary: string;
    blocks: DocumentPreviewBlock[];
    total: number;
    offset: number;
  };
  quality?: {
    status: "complete" | "needs_parse" | "needs_ocr" | "partial";
    complete: boolean;
    page_count: number;
    completed_pages: number[];
    pending_pages: number[];
    pending_parse_pages?: number[];
    failed_pages: number[];
    review_pages: number[];
    review_required: boolean;
    deferred_pages?: number[];
    retry_after_seconds?: number;
  } | null;
}

export interface DocumentPreviewBlock {
  kind: string;
  text?: string;
  image_id?: string;
  caption?: string;
  summary?: string;
  width?: number;
  height?: number;
  cells?: { row: number; text: string; col_span: number; row_span: number }[];
}

export interface LegalFactRequirement {
  key: string;
  label: string;
  purpose: string;
  decisive: boolean;
}
export interface LegalRule {
  id: string;
  title: string;
  module: "qa01" | "qa02" | "qa03" | "qa04" | "qa05" | "qa06";
  enabled: boolean;
  execution: "model" | "manual";
  domains: string[];
  jurisdictions: string[];
  condition: string;
  instructions: string;
  required_facts: LegalFactRequirement[];
  output_requirements: string;
  review_required: boolean;
  source_reference: string;
}
export interface LegalPolicy {
  enabled: boolean;
  jurisdictions: string[];
  question_limit: number;
  max_rounds: number;
  retrieval_limit: number;
  rules: LegalRule[];
}
export interface LegalPolicyDefaults {
  policy: LegalPolicy;
  program_checks: { id: string; title: string; description: string }[];
  modules: { id: LegalRule["module"]; title: string }[];
}
export interface LegalFact {
  key: string;
  label: string;
  value: string | number | null;
  source: string;
  confirmed: boolean;
  extraction_confidence: "unknown" | "low" | "medium" | "high";
  evidence_status: "unverified" | "verified" | "conflict";
  evidence_refs: string[];
  conflicts: {
    value: string | number | null;
    source: string;
    evidence_refs: string[];
  }[];
  resolved_conflicts?: {
    value: string | number | null;
    source: string;
    evidence_refs: string[];
  }[];
  revision: number;
  review_note?: string | null;
}
export interface LegalDeadline {
  id: string;
  label: string;
  status: "calculated" | "conditional" | "unavailable";
  date: string | null;
  timezone: string | null;
  as_of?: string;
  remaining_days?: number | null;
  risk?: "overdue" | "imminent" | "upcoming" | "unknown";
  assumptions: string[];
  missing_prerequisites: string[];
  citation_ids: string[];
  inputs: Record<string, unknown>;
}
export interface BusinessContext {
  actors: {
    id: string;
    name: string;
    entity_type: string;
    jurisdiction: string | null;
    roles: string[];
    identity_status: "identified" | "unknown";
    sources: string[];
  }[];
  relationships: {
    from_actor_id: string;
    to_actor_id: string;
    relationship: string;
    sources: string[];
  }[];
  credentials: {
    id: string;
    actor_id: string;
    credential_type: string;
    scope: string;
    proof: string;
    status: "reported_completed" | "documented" | "unknown";
    sources: string[];
  }[];
}
export interface LegalObligation {
  id: string;
  matter: string;
  kind: "statutory" | "contractual" | "right";
  legal_actor_id: string | null;
  performer_id: string | null;
  registrant_id: string | null;
  business_responsible_id: string | null;
  trigger: string;
  citation_ids: string[];
  prerequisites: string[];
  completion_proof: string[];
  status: "completed" | "pending" | "not_applicable" | "unconfirmed";
  credential_ids: string[];
  fact_sources: string[];
  validity_status?: "reference_checked" | "unconfirmed" | "historical";
  fact_status?: "sourced" | "unknown";
  reason: string;
}
export interface LegalIssue {
  id: string;
  question: string;
  kind: "information" | "legal";
  domains: string[];
  jurisdictions: string[];
  fact_keys: string[];
  depends_on?: string[];
  missing_facts: LegalFactRequirement[];
  conditions: {
    label: string;
    state: "met" | "not_met" | "unknown" | "not_applicable";
    fact_keys: string[];
    fact_sources?: string[];
    citation_ids: string[];
    reason: string;
  }[];
  status: "supported" | "unsupported" | "conditional" | "undetermined";
  conclusion: string;
  explanation: string;
  rule_interpretation?: string;
  exceptions?: string[];
  obligations?: LegalObligation[];
  citations: Citation[];
  deadlines: LegalDeadline[];
  actions: {
    id: string;
    title: string;
    nature:
      | "mandatory"
      | "conditional"
      | "recommended"
      | "commercial"
      | "unverified";
    details: string;
    obligation_ids?: string[];
    responsible_actor_id?: string | null;
    timing?: string;
    prerequisites?: string[];
    completion_proof?: string[];
  }[];
  gaps: string[];
  needs_review: boolean;
}
export interface LegalResult {
  question: string;
  task_type?:
    | "lookup"
    | "consultation"
    | "dispute"
    | "contract_review"
    | "business_plan";
  business_context?: BusinessContext;
  issues: LegalIssue[];
  facts: LegalFact[];
  followups: LegalFactRequirement[];
  sections: { id: string; issue_id: string; title: string; content: string }[];
  trace: TraceStep[];
  gaps: string[];
  assistant_mode: "demo" | "live";
  output: string;
  citations: Citation[];
}
export interface LegalAnalysisSummary {
  id: string;
  case_id: string;
  stage_id: string;
  question: string;
  revision: number;
  status:
    | "running"
    | "completed"
    | "needs_input"
    | "needs_review"
    | "failed"
    | "cancelled";
  created_at: string;
  updated_at: string;
  affected_issue_ids: string[];
  error: string | null;
}
export interface LegalSection {
  id: string;
  issue_id: string;
  title: string;
  content: string;
  generated_content: string;
  suggested_content: string | null;
  edited: boolean;
  needs_review: boolean;
}
export interface LegalAnalysis extends LegalAnalysisSummary {
  result: LegalResult;
  sections: LegalSection[];
}
export interface LegalHistory {
  facts_truncated?: boolean;
  facts: LegalFact[];
  analyses: LegalAnalysisSummary[];
  total: number;
}
export interface LegalFactInput {
  expected_revision: number;
  label: string;
  value: string | number | null;
  confirmed: boolean;
  evidence_status: LegalFact["evidence_status"];
  evidence_refs: string[];
  review_note: string;
}
export type LegalStreamEvent =
  | { event: "status"; data: { label: string } }
  | { event: "step"; data: TraceStep };
