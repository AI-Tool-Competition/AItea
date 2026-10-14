"use client";

import { useEffect, useRef, useState } from "react";

import {
  errorMessage,
  getLegalAnalysis,
  getLegalHistory,
  getLegalVersions,
  saveLegalFact,
  saveLegalSection,
  streamLegalAnalysis,
  TeaApiError,
  teaRequest,
} from "./api";
import {
  type CaseDetail,
  type LegalAnalysis,
  type LegalFact,
  type LegalFactInput,
  type LegalHistory,
  type LegalSection,
  type TraceStep,
} from "./types";

interface SectionDraft {
  analysis_id: string;
  section_id: string;
  issue_id: string;
  title: string;
  content: string;
  expected_revision: number;
}
function sectionDraftKey(analysisId: string, section: LegalSection) {
  return JSON.stringify([
    analysisId,
    section.id,
    section.issue_id,
    section.title,
  ]);
}
interface LegalDraft {
  question: string;
  selectedId: string;
  facts: Record<string, LegalFactInput>;
  sections: Record<string, SectionDraft>;
  retry?: { key: string; clientId: string };
}
const drafts = new Map<string, LegalDraft>();
const controllers = new Set<AbortController>();
let generation = 0;
export function clearLegalDrafts() {
  generation++;
  for (const controller of controllers) controller.abort();
  controllers.clear();
  drafts.clear();
}
export function legalFactInput(fact: LegalFact): LegalFactInput {
  return {
    expected_revision: fact.revision,
    label: fact.label,
    value: fact.value,
    confirmed: fact.confirmed,
    evidence_status: fact.evidence_status,
    evidence_refs: [...fact.evidence_refs],
    review_note: fact.review_note ?? "",
  };
}

// The workbench mounts this hook with an account/case/stage key. Every async
// operation also checks that scope and the logout generation before applying.
export function useLegalWorkbench(
  accountId: string,
  caseId: string,
  stageId: string,
  caseRevision: number,
  onUpdate?: (record: CaseDetail) => void,
  analysisId?: string,
  analysisRevision?: number,
) {
  const scope = JSON.stringify([accountId, caseId, stageId]);
  const [draft, setDraft] = useState<LegalDraft>(() => ({
    ...(drafts.get(scope) ?? {
      question: "",
      selectedId: "",
      facts: {},
      sections: {},
    }),
    ...(analysisId ? { selectedId: analysisId } : {}),
  }));
  const updateCase = useRef(onUpdate);
  updateCase.current = onUpdate;
  const latest = useRef(draft);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const session = useRef(generation);
  const alive = useRef(false);
  const requests = useRef(new Set<AbortController>());
  const mutation = useRef<AbortController | null>(null);
  const [history, setHistory] = useState<LegalHistory>({
    facts: [],
    analyses: [],
    total: 0,
  });
  const [analysis, setAnalysis] = useState<LegalAnalysis>();
  const [page, setPage] = useState(0);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [label, setLabel] = useState("");
  const [steps, setSteps] = useState<TraceStep[]>([]);
  const [versions, setVersions] = useState<{
    items: { revision: number; created_at: string }[];
    total: number;
    offset: number;
  }>();
  const [versionsLoading, setVersionsLoading] = useState(false);
  const versionSequence = useRef(0);
  const valid = (abort?: AbortController) =>
    alive.current &&
    scopeRef.current === scope &&
    session.current === generation &&
    !abort?.signal.aborted;
  function controller() {
    const abort = new AbortController();
    requests.current.add(abort);
    controllers.add(abort);
    return abort;
  }
  function release(abort: AbortController) {
    requests.current.delete(abort);
    controllers.delete(abort);
  }
  function update(edit: (current: LegalDraft) => LegalDraft) {
    if (!valid()) return;
    const next = edit(latest.current);
    latest.current = next;
    drafts.set(scope, next);
    setDraft(next);
  }
  function adopt(value: LegalAnalysis) {
    if (value.case_id !== caseId || value.stage_id !== stageId)
      throw new Error("返回的分析不属于当前业务节点，请重新读取。");
    setAnalysis(value);
  }
  function fail(e: unknown) {
    setError(
      e instanceof TeaApiError && e.status === 409
        ? `${errorMessage(e)}。你的输入已保留，请读取最新版本并核对差异后再保存。`
        : errorMessage(e),
    );
  }
  useEffect(() => {
    alive.current = true;
    const active = requests.current;
    return () => {
      alive.current = false;
      for (const abort of active) {
        abort.abort();
        controllers.delete(abort);
      }
      active.clear();
    };
  }, [scope]);
  useEffect(() => {
    // A different saved node report replaces only the transient run record.
    setSteps((current) => (current.length ? [] : current));
  }, [analysisId, analysisRevision]);
  useEffect(() => {
    if (analysisId)
      update((current) => ({ ...current, selectedId: analysisId }));
    // A new node report changes selection without clearing any unsaved edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysisId]);
  useEffect(() => {
    const abort = controller();
    setLoading(true);
    setHistoryError("");
    void getLegalHistory(caseId, stageId, page * 20, abort.signal)
      .then((value) => {
        if (!valid(abort)) return;
        setHistory(value);
        if (!latest.current.selectedId && value.analyses[0])
          update((current) => ({
            ...current,
            selectedId: value.analyses[0]!.id,
          }));
      })
      .catch((e) => {
        if (valid(abort)) setHistoryError(errorMessage(e));
      })
      .finally(() => {
        if (valid(abort)) setLoading(false);
        release(abort);
      });
    return () => {
      abort.abort();
      release(abort);
    };
    // Scope is remounted by LegalWorkbench; revisions refresh server values only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, caseRevision, page, reload, analysisRevision]);
  useEffect(() => {
    setVersions(undefined);
    versionSequence.current++;
    setVersionsLoading(false);
    if (!draft.selectedId) {
      setAnalysis(undefined);
      return;
    }
    const abort = controller();
    setDetailLoading(true);
    const selectedId = draft.selectedId;
    void getLegalAnalysis(caseId, selectedId, abort.signal)
      .then((value) => {
        if (valid(abort) && latest.current.selectedId === selectedId)
          adopt(value);
      })
      .catch((e) => {
        if (valid(abort)) fail(e);
      })
      .finally(() => {
        if (valid(abort)) setDetailLoading(false);
        release(abort);
      });
    return () => {
      abort.abort();
      release(abort);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, caseRevision, draft.selectedId, reload, analysisRevision]);
  useEffect(() => () => mutation.current?.abort(), [caseRevision]);
  const dirty =
    !!draft.question ||
    Object.keys(draft.facts).length > 0 ||
    Object.keys(draft.sections).length > 0;
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  function begin(name: string) {
    if (mutation.current) return null;
    const abort = controller();
    mutation.current = abort;
    setBusy(name);
    setError("");
    return abort;
  }
  function finish(abort: AbortController) {
    release(abort);
    if (mutation.current === abort) {
      mutation.current = null;
      if (valid()) setBusy("");
    }
  }
  async function run(refresh = false) {
    if (loading || historyError || detailLoading || (refresh && !analysis))
      return;
    const question = latest.current.question.trim();
    if (!refresh && !question) return;
    const abort = begin("run");
    if (!abort) return;
    const body = refresh
      ? { expected_revision: analysis!.revision }
      : { stage_id: stageId, question };
    const requestKey = JSON.stringify([refresh ? analysis!.id : null, body]);
    const clientId =
      latest.current.retry?.key === requestKey
        ? latest.current.retry.clientId
        : crypto.randomUUID();
    update((current) => ({ ...current, retry: { key: requestKey, clientId } }));
    setLabel("正在分析资料与问题…");
    setSteps([]);
    try {
      const value = await streamLegalAnalysis(
        caseId,
        { ...body, client_request_id: clientId },
        {
          analysisId: refresh ? analysis!.id : undefined,
          signal: abort.signal,
          onEvent: (event) => {
            if (!valid(abort)) return;
            if (event.event === "status") setLabel(event.data.label);
            else
              setSteps((current) => [
                ...current.filter((item) => item.id !== event.data.id),
                event.data,
              ]);
          },
        },
      );
      if (!valid(abort)) return;
      adopt(value);
      setSteps(value.result.trace);
      update((current) => ({
        ...current,
        selectedId: value.id,
        retry: undefined,
        question:
          !refresh &&
          !["failed", "cancelled", "running"].includes(value.status) &&
          current.question.trim() === question
            ? ""
            : current.question,
      }));
      setLabel("");
      setPage(0);
      setReload((value) => value + 1);
    } catch (e) {
      if (!valid()) return;
      if (abort.signal.aborted)
        setLabel("已停止。结果尚未完整生成，可从历史记录重新分析。");
      else {
        fail(e);
        setLabel("分析未完成，可使用保留的问题重试。");
      }
      setReload((value) => value + 1);
    } finally {
      finish(abort);
    }
  }
  function editFact(fact: LegalFact, patch: Partial<LegalFactInput>) {
    update((current) => ({
      ...current,
      facts: {
        ...current.facts,
        [fact.key]: {
          ...(current.facts[fact.key] ?? legalFactInput(fact)),
          ...patch,
        },
      },
    }));
  }
  async function saveFact(fact: LegalFact) {
    const input = latest.current.facts[fact.key] ?? legalFactInput(fact);
    const abort = begin(`fact:${fact.key}`);
    if (!abort) return;
    try {
      const value = await saveLegalFact(caseId, fact.key, input, abort.signal);
      if (!valid(abort)) return;
      setHistory((current) => ({
        ...current,
        facts: [
          ...current.facts.filter((item) => item.key !== fact.key),
          value.fact,
        ],
      }));
      update((current) => {
        const facts = { ...current.facts };
        if (JSON.stringify(facts[fact.key]) === JSON.stringify(input))
          delete facts[fact.key];
        else {
          const retained = facts[fact.key];
          if (retained)
            facts[fact.key] = {
              ...retained,
              expected_revision: value.fact.revision,
            };
        }
        return { ...current, facts };
      });
      setReload((value) => value + 1);
      if (updateCase.current) {
        try {
          const refreshed = await teaRequest<CaseDetail>(
            `/cases/${encodeURIComponent(caseId)}`,
            { signal: abort.signal },
          );
          if (valid(abort) && refreshed.id === caseId)
            updateCase.current(refreshed);
        } catch (e) {
          if (valid(abort))
            setError(`事实已保存，但业务记录暂未刷新。${errorMessage(e)}`);
        }
      }
    } catch (e) {
      if (valid(abort)) fail(e);
    } finally {
      finish(abort);
    }
  }
  function editSection(section: LegalSection, content: string) {
    if (!analysis) return;
    const key = sectionDraftKey(analysis.id, section);
    update((current) => ({
      ...current,
      sections: {
        ...current.sections,
        [key]: {
          ...(current.sections[key] ?? {
            analysis_id: analysis.id,
            section_id: section.id,
            issue_id: section.issue_id,
            title: section.title,
            expected_revision: analysis.revision,
          }),
          content,
        },
      },
    }));
  }
  async function saveSection(section: LegalSection, accept = false) {
    if (!analysis) return;
    const key = sectionDraftKey(analysis.id, section);
    const input = latest.current.sections[key] ?? {
      content: section.content,
      expected_revision: analysis.revision,
    };
    if (accept && latest.current.sections[key]) return;
    const abort = begin(`section:${section.id}`);
    if (!abort) return;
    try {
      const value = await saveLegalSection(
        caseId,
        analysis.id,
        section.id,
        {
          content: input.content,
          expected_revision: input.expected_revision,
          ...(accept
            ? {
                content: section.suggested_content ?? "",
                accept_suggestion: true,
              }
            : {}),
        },
        abort.signal,
      );
      if (!valid(abort)) return;
      adopt(value);
      update((current) => {
        const sections = { ...current.sections };
        if (JSON.stringify(sections[key]) === JSON.stringify(input))
          delete sections[key];
        return { ...current, sections };
      });
      setReload((value) => value + 1);
    } catch (e) {
      if (valid(abort)) fail(e);
    } finally {
      finish(abort);
    }
  }
  async function loadVersions(offset = 0) {
    if (!analysis) return;
    const sequence = ++versionSequence.current;
    const abort = controller();
    setVersionsLoading(true);
    try {
      const result = await getLegalVersions(
        caseId,
        analysis.id,
        offset,
        abort.signal,
      );
      if (valid(abort) && sequence === versionSequence.current)
        setVersions({ ...result, offset });
    } catch (e) {
      if (valid(abort) && sequence === versionSequence.current) fail(e);
    } finally {
      if (valid(abort) && sequence === versionSequence.current)
        setVersionsLoading(false);
      release(abort);
    }
  }
  return {
    draft,
    history,
    analysis,
    page,
    loading,
    detailLoading,
    busy,
    error,
    historyError,
    label,
    steps,
    versions,
    versionsLoading,
    editQuestion: (question: string) =>
      update((current) => ({ ...current, question })),
    choose: (selectedId: string) => {
      if (busy) return;
      setAnalysis(undefined);
      setSteps([]);
      update((current) => ({ ...current, selectedId }));
    },
    setPage,
    run,
    stop: () => mutation.current?.abort(),
    reload: () => {
      setError("");
      setReload((value) => value + 1);
    },
    editFact,
    saveFact,
    editSection,
    saveSection,
    sectionDraft: (section: LegalSection) =>
      analysis
        ? draft.sections[sectionDraftKey(analysis.id, section)]
        : undefined,
    retainedSections: analysis
      ? Object.entries(draft.sections).filter(
          ([key, edit]) =>
            edit.analysis_id === analysis.id &&
            !analysis.sections.some(
              (section) => sectionDraftKey(analysis.id, section) === key,
            ),
        )
      : [],
    discardSectionDraft: (key: string) =>
      update((current) => {
        const sections = { ...current.sections };
        delete sections[key];
        return { ...current, sections };
      }),
    loadVersions,
    rebaseSection: (section: LegalSection) => {
      if (analysis)
        update((current) => {
          const key = sectionDraftKey(analysis.id, section);
          const existing = current.sections[key];
          return existing
            ? {
                ...current,
                sections: {
                  ...current.sections,
                  [key]: { ...existing, expected_revision: analysis.revision },
                },
              }
            : current;
        });
    },
  };
}
