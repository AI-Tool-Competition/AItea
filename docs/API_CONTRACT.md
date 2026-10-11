# Tea API v1 (implementation contract)

Base `/api/tea`. Same-origin HTTP-only cookie session. JSON objects snake_case. Errors `{error:{code,message,request_id?}}`; 401 unauthenticated, 404 non-owned resources, 409 stale revisions/locked stage, 422 validation. All returned dates ISO UTC. API owns all state; no browser-only business data. List endpoints return `{items:[],total:number}` with bounded limit/offset. `GET /bootstrap` supplies `{user,templates:Template[],cases:CaseSummary[],capabilities}`.

## Authentication
- POST `/auth/demo` {} creates/reuses the current demo session (isolated per browser, seed an illustrative case); returns User.
- POST `/auth/register` {name,email,password}; POST `/auth/login` {email,password}; GET `/auth/me`; responses User `{id,name,email,is_demo}`.
- POST `/auth/logout` -> 204. GET `/health` -> `{status,version,assistant_mode,retrieval_mode,parser}` no secrets.
- capabilities `{assistant_mode:'demo'|'live',retrieval_mode:'lexical'|'hybrid',parser:string}`.

## Template format
Template = `{id,version,title,description,origin,destination,is_demo,modes:Mode[],stages:StageDefinition[],agent:AgentConfig}`.
Mode = `{id,name,description,enabled}`. Demo modes use 模式 A / 模式 B (待配置), not invented trade modes.
StageDefinition = `{id,name,actor,description,tasks:TaskDefinition[],analysis?:AgentConfig|null}`. Missing/null `analysis` inherits `Template.agent`; an explicit configuration replaces it for this stage. Demo names can be 业务资料 / 对接准备 / 材料交付; they are illustrative preparation phases, not claimed legal requirements. actor names 企业 / 对接主体 / 接收主体, configurable.
TaskDefinition = `{id,title,description,required:boolean,fields:FieldDefinition[],require_document:boolean,require_confirmation:boolean,output_template?:string}`.
FieldDefinition = `{key,label,type:'text'|'number'|'textarea',required:boolean,min?:number,placeholder?:string}`.
AgentConfig = `{language:string,instructions:string,retrieval_limit:number,steps:{id,type:'retrieve'|'check'|'guide'|'draft'|'verify',label,enabled:boolean,instructions?:string}[]}`. Steps have unique IDs within their workflow and at least one enabled step; per-step instructions default to `""` and are limited to 3000 characters.
- GET `/templates` -> list; GET `/templates/{id}` -> latest Template.
- POST `/templates/validate` {template:object} -> `{valid:boolean,errors:{path,message}[],preview:{stages,tasks}}`. Incomplete configuration is accepted for structured validation; the request wrapper itself rejects unknown fields.
- GET `/templates/drafts?limit=50&offset=0` -> `{items:{id,title,revision,updated_at}[],total:number}`. Lists all saved drafts for the current account, including unpublished IDs; does not return template bodies. Titles are at most 200 characters, with the ID as a fallback for absent, non-text or blank titles. Pagination is bounded to 200, ordered by update time descending, then ID descending. Fetch a selected draft through `/templates/{id}/draft`.
- GET `/templates/{id}/draft` -> `{template:object|null,revision:number,updated_at:string|null}`. A known template without a draft returns null/revision 0; an unknown ID returns 404.
- PUT `/templates/{id}/draft` {template:object,expected_revision:number} -> same draft shape. Supports incomplete forms and new IDs, requires the payload ID to match the route, and caps the JSON template at 512 KB. Owner-scoped revisions start at 1; an outdated revision returns 409 `stale_draft`. A new unpublished draft is retrievable by ID but is not added to the published `/templates` list.
- POST `/templates/publish` {template:Template,expected_draft_revision?:number} -> Template with a new immutable version. `template.version` must match the latest published version (or 1 for a new ID); otherwise returns 409 `stale_template`. When `expected_draft_revision` is supplied, publishing checks the saved draft revision, replaces its contents with the published template and increments its revision in the same transaction. Existing callers can omit it. New IDs publish as version 1. Owner scoped; existing cases keep their frozen configuration. Unsupported fields/rules are rejected.
- POST `/templates/debug` {template:Template,input?:string,stage_id?:string,task_values?:Record<task_id,Record<field_key,string|number>>,stop_after_step_id?:string} -> `{output:string,trace:TraceStep[],checks:Check[],citations:Citation[],stage_id:string,assistant_mode:'demo'|'live',retrieval_mode:'lexical'|'hybrid'}`. Defaults to the first stage and empty input/values. Checks use all tasks in the selected stage, with empty evidence/confirmation; sample values are validated by the same field rules as task editing. Unknown stages/tasks/fields/steps and invalid numbers return 422. A stop point runs through that step and marks later steps skipped. Debug uses only this account's library for retrieval and never writes cases, tasks, documents or drafts.

Debug, proactive analysis and stage chat share the same ordered runner. A guide step sees only results produced by preceding steps. Final citations belong to the last executed guide step; a later retrieval is visible in its trace but cannot retroactively support that guidance. If no guide executed, citations contain the actual retrieved results. Retrieve-step instructions provide a search focus; guide-step instructions enter the configured model request. Check steps execute server rules; their free-text instructions do not create new rules. Draft/verify steps explicitly skip actual mutations and point to the dedicated task actions. Without a model, guide steps run the labelled rule-only demo; free-text writing instructions are not represented as model execution. Provider calls are bounded in time, response size and concurrency; a failed step is visible and stops later steps.

## Cases / tasks
CaseSummary = `{id,title,company,product,mode_id,template_version,created_at,updated_at,completed_tasks,total_tasks,current_stage_id}`.
CaseDetail extends CaseSummary with `{template:Template,revision:number,stages:StageState[],facts:Fact[],documents:Document[],artifacts:Artifact[],events:AuditEvent[]}`.
StageState = `{id,name,actor,description,status:'locked'|'active'|'completed',tasks:TaskState[]}`.
TaskState = TaskDefinition + `{status:'pending'|'in_progress'|'needs_input'|'completed',needs_review:boolean,revision:number,values:Record<string,string|number>,evidence_ids:string[],confirmed:boolean,verification:Verification|null}`.
Fact = `{key,label,value:string|number,source:string,confirmed:boolean,updated_at}`.
Check = `{id,label,status:'passed'|'missing'|'conflict',reason:string,evidence_refs:string[],task_id?:string,task_title?:string}`. Stage workflow results identify the checked task when applicable; cross-document reported conflicts can omit it.
Verification = `{id,status:'passed'|'needs_input',checks:Check[],created_at,task_revision:number}`.
AuditEvent = `{id,type,title,detail,created_at}`.
- GET `/cases` -> summaries list. POST `/cases` {title,company,product,mode_id,template_id} -> CaseDetail.
- GET `/cases/{id}` -> CaseDetail; all mutations below return updated CaseDetail unless noted.
- PATCH `/cases/{id}/tasks/{task_id}` {values:Record<string,string|number>,evidence_ids?:string[],expected_revision:number}.
- POST `/cases/{id}/tasks/{task_id}/verify` {expected_revision:number,confirm:boolean}. Gate must examine current facts/documents, nonempty fields and evidence validity; required predecessors must pass; facts/evidence edit invalidates old confirmation. Non-required tasks do not gate unless configured.
- GET `/cases/{id}/events` -> list audit events.
- POST `/cases/{id}/stages/{stage_id}/analysis` {expected_revision:number,retry?:boolean} -> `{id,stage_id,case_revision,status:'completed'|'failed',output,trace:TraceStep[],checks:Check[],citations:Citation[],created_at,assistant_mode:'demo'|'live',retrieval_mode?:'lexical'|'hybrid'}`. Called when entering an unlocked stage. Requires the current case revision; locked stages return 409 `stage_locked`, stale revisions 409 `stale_revision`, and unowned resources 404. Analysis never changes task state, facts, messages or case revision.

Analysis is persisted per case/stage/input fingerprint: frozen configuration, task/confirmed facts, applicable document versions/review revisions, artifacts, provider configuration and applicability date. Repeated calls return the stored result (including its original `case_revision`); chat-only revision changes do not trigger another analysis. Failed results require explicit `retry:true` to execute again. Concurrent requests return 409 `analysis_in_progress`; an abandoned claim expires after 30 minutes. Retrieval and generation execute outside write transactions. If relevant inputs change before completion, no stale result is published and the request returns 409 `analysis_stale`.

## Conversation
Message = `{id,role:'user'|'assistant',content,stage_id,task_id:string|null,created_at,citations:Citation[],trace:TraceStep[]}`.
Citation = `{document_id,title,excerpt,locator:object,source_url?:string,document_version?:number}`.
TraceStep = `{id,label,status:'completed'|'skipped'|'failed',detail:string,input?:string,output?:string}`. Input/output fields are bounded public operation summaries, not raw chain of thought. Skipped operations are never labelled completed.
- GET `/cases/{id}/messages?stage_id=...` -> list.
- POST `/cases/{id}/messages` {stage_id,task_id?:string,content,client_message_id:string} -> `{messages:Message[],case:CaseDetail}`. Message ID makes retries idempotent. Include relevant case facts and retrieved citations; demo assistant clearly labeled in capabilities and trace. Do not pretend to invoke a model when unconfigured. Grounded factual conflict handling and progress reporting should trigger questions, never trust free text as completed state. A live provider is optional via server environment, errors visible.
- POST `/cases/{id}/messages/stream` accepts the same input and cookie authentication, and returns `text/event-stream` with `X-Accel-Buffering: no` and `Cache-Control: no-store, no-transform`. The common response middleware preserves `no-transform` so the Next proxy does not buffer small frames in gzip compression. Ownership, stage/task and completed-request conflicts are checked before streaming headers. Each SSE frame has an `event:` name and one JSON `data:` value:
  - `status`: `{label,step_id?}` describes the current public operation; the first frame acknowledges the request immediately.
  - `start`: `{step_id}` replaces the provisional answer when a guide step begins. Multiple guide steps do not concatenate unrelated answers.
  - `delta`: `{text}` appends text from the configured provider's actual incremental response. Rule-only demo guidance arrives when ready, without simulated token delays.
  - `done`: `{messages:Message[],case:CaseDetail}` is the authoritative, persisted result, including citations, traces and any concurrent-data-change warning. Replace provisional text and deduplicate by message ID. Replaying a completed `client_message_id` returns the stored messages without calling the provider again.
  - `error`: `{code,message}` terminates an unsuccessful stream. An EOF without `done` is an interruption, never a successful reply. Before headers, errors use the ordinary HTTP/JSON envelope.

Both message endpoints share a per-process limit of four generating conversations and one active request per case (`chat_busy` / `message_in_progress`). Streaming errors, cancellation before commit, and incomplete upstream streams do not persist a partial message pair. Disconnect cancels the upstream async model read, including while waiting for its first byte; synchronous retrieval stops at its next workflow checkpoint. A stop racing with a successful commit may leave a complete stored result; retry the same message ID to recover it. No automatic retries or cross-process run recovery are provided. Completed turns retain the existing transactional idempotency guarantee. Streaming uses a bounded queue, heartbeat comments during quiet work, the existing 45-second/1-MB provider bounds and a 20,000-character answer limit. The browser retains provisional text on interruption, offers explicit retry with the original ID, and times out after 60 seconds with no frames.

## Documents / library
Document = `{id,title,filename,role:'primary'|'secondary',status:'ready'|'failed'|'stored'|'processing',mime_type,size,version,case_id:string|null,parent_id:string|null,tags:string[],created_at,source_url:string|null,error:string|null,chunk_count:number}`.
- GET `/documents?case_id=...&role=primary|secondary&parent_id=...&q=...&limit=100&offset=0` -> owned list. Omit case_id = library; omit the new filters to preserve the existing list. `q` matches literal title/filename text (max 200 characters). A parent must be an owned primary in the same scope; its children are secondary only. Pagination is bounded to 200 and ordered by creation time descending, then ID descending.
- POST `/documents` multipart file + optional case_id, role(default primary), parent_id, tags(JSON array) -> Document. Max 20 MB; safe filenames; secondary requires valid parent in same owner/scope and must never parse/index. Uploaded HTML download as attachment only.
- GET `/documents/{id}?element_offset=0&chunk_offset=0&limit=100` -> `{document:Document,elements:object[],chunks:object[],element_total:number,chunk_total:number,element_offset:number,chunk_offset:number,limit:number,parser:string|null,warnings:string[]}`. Element and chunk offsets are independent; limit is 1–200. Chunk embeddings are never returned. Secondary attachments have no parsed elements/chunks and a null parser; their original remains downloadable.
- GET `/documents/{id}/download` -> original bytes (authenticated).
- POST `/search` {query,case_id?:string,limit?:number} -> `{items:Citation[],mode:'lexical'|'hybrid'}`. Case queries only case docs + explicit library docs owned by same account, never other cases.

## Artifacts
Artifact = `{id,case_id,task_id,title,content,version:number,confirmed:boolean,created_at,updated_at}`.
- POST `/cases/{id}/artifacts` {task_id,title?:string} -> Artifact (draft generated from configured template + facts with missing info placeholders).
- PATCH `/artifacts/{id}` {title,content,expected_version:number} -> Artifact new revision; editing invalidates confirmation and dependent completion.
- POST `/artifacts/{id}/confirm` {expected_version:number} -> Artifact.
- GET `/artifacts/{id}/versions` -> list Artifact snapshots; GET `/artifacts/{id}/download` -> UTF8 Markdown attachment.

## Source collection
Source = `{id,name,url,enabled:boolean,interval_hours:number,tags:string[],last_fetched_at:string|null,status:string,last_error:string|null}`.
- GET `/sources` -> list; POST `/sources` {name,url,enabled,interval_hours,tags} -> Source; PATCH `/sources/{id}` same fields optional -> Source.
- POST `/sources/{id}/fetch` -> `{source:Source,documents:Document[]}`. Bounded real HTTPS fetch, originals saved, content hash/version dedup, related secondary attachments stored only. Periodic runner may use same service. No default external sources invented; empty state explains adding an official source.

## Parser service contract (pure Python, no DB)
`app.ingestion.parse_file(filename: str, content: bytes, mime_type: str = '', *, role: str='primary', parser: str='basic') -> dict` returns `{parser,elements:[{element_key,kind,content_text,section_path,locator}],chunks:[{id,text,section_path,locator}],warnings:[]}`. Secondary raises ValueError (backend stores without calling). Defaults basic TXT/MD/HTML/DOCX/PDF; Docling explicitly optional. Bounded size/decompression/page limits. Extract actual original parser into `app/vendor/geo_parser` with provenance and relative import.
`app.ingestion.lexical_search(query: str, chunks: list[dict], limit: int=5) -> list[dict]`: chunks include text + metadata; returns hits with score, preserves metadata; irrelevant query no arbitrary hits.
`app.ingestion.embed_texts(texts:list[str]) -> list[list[float]] | None`: env configured OpenAI-compatible provider, no fake vector fallbacks; explicit errors and dimension validation.
`app.ingestion.fetch_source(url: str, *, max_attachments:int=5) -> dict`: `{url,filename,mime_type,content:bytes,attachments:[{url,filename,mime_type,content:bytes}],fetched_at}` HTTPS/public address pinned connection, safe redirects, size/time limits, same-origin direct recognized-file links only. Called off request event loop.

## Document review and applicability

Document responses also include `metadata`, `metadata_revision` and `reviewed_at`. Metadata is `{kind:'law'|'guidance'|'case'|'business'|'demo',jurisdiction,language,published_at:date|null,effective_from:date|null,effective_to:date|null,review_status:'draft'|'published'}`. These are classification/review data, never claims that a source is legally authoritative.

`PATCH /documents/{id}/metadata` takes `expected_revision` and any metadata fields, returns the updated Document. Stale revisions return 409; inverted date ranges and secondary attachments return 422. Changes are audit-versioned. Editing applicability without explicit renewed approval resets it to draft; associated case tasks and downstream confirmations require review.

`POST /search` accepts optional `as_of` (YYYY-MM-DD); default UTC today. Only the newest version of a source/file identity can enter retrieval, and it must be parsed, reviewed and within its known effective dates. A new pending or failed version suppresses the older version rather than silently quoting superseded text. Secondary attachments are never indexed. Search citations carry `metadata` and `metadata_revision`. Fetched sources begin as law/draft; ordinary uploads begin as business/published and are never represented as legislation. Unknown dates mean applicability is unknown, not legally verified.

## Local configuration

The validated startup JSON file defaults to `config/workflows/china-indonesia.json` and is overridable via `TEA_TEMPLATE_FILE`. A user's published versions take priority for that user's new cases; frozen case snapshots remain unchanged. File changes are loaded on service restart.
