# Tea export workbench implementation plan

**Goal:** Deliver the approved configuration-driven Chinese tea-export demo as a working full-stack application with persistent business cases, a DeerFlow-based frontend, verified task gates, authoring, and attributable document retrieval.

**Architecture:** Modular Python service owns accounts, configuration versions, business state and evidence; the actual DeerFlow Next.js frontend is copied into frontend/ and extended through an isolated /tea route. The legacy document parser is extracted into a vendored package with a small adapter. Reference checkouts in tmp/ remain unchanged.

**Tech stack:** DeerFlow Next.js/React/TypeScript/Tailwind UI, FastAPI/Pydantic, SQLite WAL for the single-node local demo, optional OpenAI-compatible model and embedding endpoints, Docling optional and bounded basic parsers by default.

## Approved invariants
- China to Indonesia; mode names and legal content remain configurable, explicitly illustrative demo templates.
- One business case groups stage conversations. Stages serial, tasks parallel. Business state is independent from assistant output.
- Required conditions, evidence and current revisions gate completion. Case facts, file changes and artifact versions are auditable.
- Template publication immutable; cases retain their snapshot. Templates edited with forms/JSON in a separate legal studio.
- Primary documents parsed and indexed, linked secondary documents stored without parsing/indexing.
- Legal sources have provenance/validity metadata; no fictitious laws or citations. No model key means explicitly labeled demo assistant.
- Account isolation, bounded uploads/network calls, restart persistence, predictable errors, accessible and responsive UI.

## Ownership and steps
1. Root: establish API contract, independent directories, copy original frontend, development/deployment scripts, documentation.
2. Backend agent: test missing behavior first; implement auth and versioned template/case store, task state machine, conversations/assistant adapter, document and artifact endpoints, isolation and stale revision checks. Run backend tests.
3. Ingestion agent: extract actual parser with provenance; implement bounded basic formats, lexical retrieval, optional embedding provider, SSRF-resistant crawler and tests. No API/database edits.
4. Frontend agent: extend DeerFlow /tea with business navigation, chat, actionable flow, task editor/verification, documents, authoring, configuration studio, account flows, mobile/error/empty states. Typecheck and build.
5. Root: integrate ingestion/backend/frontend contracts, run real browser happy/error flows, review source and fix findings, save screenshots and execution evidence.
6. Final independent specification and code review; fix issues, rerun affected checks; deliver local running preview and accurate capability/setup limitations.

## Acceptance
- Demo entry creates an isolated account and visible sample case; real register/login/logout supported.
- Configuration-backed new case, distinct stage conversations, shared facts and full state restored after refresh.
- Empty/conflicting/stale evidence never passes verification; valid conditions unlock the next stage only when all required tasks pass.
- Actual file upload/original download/parsed excerpt citation; attachment-only files do not enter retrieval.
- Artifact save/version/confirmation and configuration edit/validate/publish tested.
- HTTP errors displayed rather than fabricated success; model/provider failures explicit.
- Frontend compile, backend tests, browser desktop/mobile and workflow checks recorded.
