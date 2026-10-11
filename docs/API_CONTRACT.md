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
StageDefinition = `{id,name,actor,description,tasks:TaskDefinition[]}`. Demo names can be 业务资料 / 对接准备 / 材料交付; they are illustrative preparation phases, not claimed legal requirements. actor names 企业 / 对接主体 / 接收主体, configurable.
TaskDefinition = `{id,title,description,required:boolean,fields:FieldDefinition[],require_document:boolean,require_confirmation:boolean,output_template?:string}`.
FieldDefinition = `{key,label,type:'text'|'number'|'textarea',required:boolean,min?:number,placeholder?:string}`.
AgentConfig = `{language:string,instructions:string,retrieval_limit:number,steps:{id,type:'retrieve'|'check'|'guide'|'draft'|'verify',label,enabled:boolean}[]}`.
- GET `/templates` -> list; GET `/templates/{id}` -> latest Template.
- POST `/templates/validate` {template:Template} -> `{valid:boolean,errors:{path,message}[],preview:{stages,tasks}}`.
- POST `/templates/publish` {template:Template} -> Template with server-incremented immutable version. Owner scoped. Old cases untouched. Reject unsupported fields/rules rather than silently ignoring.
- POST `/templates/debug` {template:Template,input:string} -> `{output:string,trace:TraceStep[],checks:Check[]}` uses the actual configured runner without changing live cases.

## Cases / tasks
CaseSummary = `{id,title,company,product,mode_id,template_version,created_at,updated_at,completed_tasks,total_tasks,current_stage_id}`.
CaseDetail extends CaseSummary with `{template:Template,revision:number,stages:StageState[],facts:Fact[],documents:Document[],artifacts:Artifact[],events:AuditEvent[]}`.
StageState = `{id,name,actor,description,status:'locked'|'active'|'completed',tasks:TaskState[]}`.
TaskState = TaskDefinition + `{status:'pending'|'in_progress'|'needs_input'|'completed',needs_review:boolean,revision:number,values:Record<string,string|number>,evidence_ids:string[],confirmed:boolean,verification:Verification|null}`.
Fact = `{key,label,value:string|number,source:string,confirmed:boolean,updated_at}`.
Check = `{id,label,status:'passed'|'missing'|'conflict',reason:string,evidence_refs:string[]}`.
Verification = `{id,status:'passed'|'needs_input',checks:Check[],created_at,task_revision:number}`.
AuditEvent = `{id,type,title,detail,created_at}`.
- GET `/cases` -> summaries list. POST `/cases` {title,company,product,mode_id,template_id} -> CaseDetail.
- GET `/cases/{id}` -> CaseDetail; all mutations below return updated CaseDetail unless noted.
- PATCH `/cases/{id}/tasks/{task_id}` {values:Record<string,string|number>,evidence_ids?:string[],expected_revision:number}.
- POST `/cases/{id}/tasks/{task_id}/verify` {expected_revision:number,confirm:boolean}. Gate must examine current facts/documents, nonempty fields and evidence validity; required predecessors must pass; facts/evidence edit invalidates old confirmation. Non-required tasks do not gate unless configured.
- GET `/cases/{id}/events` -> list audit events.

## Conversation
Message = `{id,role:'user'|'assistant',content,stage_id,task_id:string|null,created_at,citations:Citation[],trace:TraceStep[]}`.
Citation = `{document_id,title,excerpt,locator:object,source_url?:string,document_version?:number}`.
TraceStep = `{id,label,status:'completed'|'skipped'|'failed',detail:string}`. No raw chain of thought.
- GET `/cases/{id}/messages?stage_id=...` -> list.
- POST `/cases/{id}/messages` {stage_id,task_id?:string,content,client_message_id:string} -> `{messages:Message[],case:CaseDetail}`. Message ID makes retries idempotent. Include relevant case facts and retrieved citations; demo assistant clearly labeled in capabilities and trace. Do not pretend to invoke a model when unconfigured. Grounded factual conflict handling and progress reporting should trigger questions, never trust free text as completed state. A live provider is optional via server environment, errors visible.

## Documents / library
Document = `{id,title,filename,role:'primary'|'secondary',status:'ready'|'failed'|'stored'|'processing',mime_type,size,version,case_id:string|null,parent_id:string|null,tags:string[],created_at,source_url:string|null,error:string|null,chunk_count:number}`.
- GET `/documents?case_id=...` -> owned list; omit case_id = library.
- POST `/documents` multipart file + optional case_id, role(default primary), parent_id, tags(JSON array) -> Document. Max 20 MB; safe filenames; secondary requires valid parent in same owner/scope and must never parse/index. Uploaded HTML download as attachment only.
- GET `/documents/{id}` -> `{document:Document,elements:object[],chunks:object[]}`.
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
