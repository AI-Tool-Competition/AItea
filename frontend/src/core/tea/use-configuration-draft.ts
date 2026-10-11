"use client";

import { useEffect, useRef, useState } from "react";

import { errorMessage, TeaApiError, teaRequest } from "./api";
import { configurationSchema } from "./configuration";
import { type Template, type TemplateDraft } from "./types";

interface MemoryDraft {
  draft: Template;
  revision: number;
  saved: string;
  updatedAt: string | null;
}
const drafts = new Map<string, MemoryDraft>();
let generation = 0;
export function clearConfigurationDrafts() {
  generation++;
  drafts.clear();
}

export function localConfigurationDrafts(ownerId: string) {
  return [...drafts.entries()]
    .filter(([key]) => key.startsWith(`${ownerId}:`))
    .map(([, value]) => value.draft);
}

export function importConfigurationDraft(
  ownerId: string,
  template: Template,
  remote: TemplateDraft | null,
) {
  drafts.set(`${ownerId}:${template.id}`, {
    draft: template,
    revision: remote?.revision ?? 0,
    saved: remote?.template ? JSON.stringify(remote.template) : "",
    updatedAt: remote?.updated_at ?? null,
  });
}

export function useConfigurationDraft(ownerId: string, initial: Template) {
  const key = `${ownerId}:${initial.id}`;
  const [state, setState] = useState<MemoryDraft>(
    () =>
      drafts.get(key) ?? {
        draft: structuredClone(initial),
        revision: 0,
        saved: JSON.stringify(initial),
        updatedAt: null,
      },
  );
  const latest = useRef(state);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [reload, setReload] = useState(0);
  const alive = useRef(false);
  const session = useRef(generation);
  const requests = useRef(new Set<AbortController>());
  const pending = useRef(false);
  const initialRef = useRef(initial);
  const dirty = JSON.stringify(state.draft) !== state.saved;

  function adopt(next: MemoryDraft) {
    if (!alive.current || session.current !== generation) return;
    latest.current = next;
    drafts.set(key, next);
    setState(next);
  }

  useEffect(() => {
    alive.current = true;
    const abort = new AbortController();
    const currentGeneration = generation;
    setLoading(true);
    setLoadError("");
    teaRequest<TemplateDraft>(
      `/templates/${encodeURIComponent(initialRef.current.id)}/draft`,
      { signal: abort.signal },
    )
      .then((remote) => {
        if (abort.signal.aborted || generation !== currentGeneration) return;
        const cached = drafts.get(key);
        if (cached && JSON.stringify(cached.draft) !== cached.saved) {
          latest.current = cached;
          setState(cached);
          if (cached.revision !== remote.revision)
            setLoadError(
              "另一窗口已保存新草稿。你的输入已保留；请先导出备份，再读取已保存草稿进行合并。",
            );
          return;
        }
        const draft = remote.template
          ? configurationSchema.parse(remote.template)
          : initialRef.current;
        const next = {
          draft,
          revision: remote.revision,
          saved: JSON.stringify(draft),
          updatedAt: remote.updated_at,
        };
        latest.current = next;
        drafts.set(key, next);
        setState(next);
      })
      .catch((error) => {
        if (abort.signal.aborted || generation !== currentGeneration) return;
        const cached = drafts.get(key);
        // A new imported configuration has no server row until its first save.
        if (
          error instanceof TeaApiError &&
          error.status === 404 &&
          cached?.revision === 0 &&
          cached.saved === ""
        )
          return;
        setLoadError(errorMessage(error));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    const active = requests.current;
    return () => {
      alive.current = false;
      abort.abort();
      for (const request of active) request.abort();
      active.clear();
    };
  }, [key, reload]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  function update(draft: Template) {
    adopt({ ...latest.current, draft });
  }
  async function save() {
    if (pending.current) throw new Error("草稿正在保存，请稍候。");
    pending.current = true;
    const submitted = latest.current;
    const abort = new AbortController();
    requests.current.add(abort);
    try {
      const remote = await teaRequest<TemplateDraft>(
        `/templates/${encodeURIComponent(submitted.draft.id)}/draft`,
        {
          method: "PUT",
          body: JSON.stringify({
            template: submitted.draft,
            expected_revision: submitted.revision,
          }),
          signal: abort.signal,
        },
      );
      adopt({
        ...latest.current,
        revision: remote.revision,
        saved: JSON.stringify(submitted.draft),
        updatedAt: remote.updated_at,
      });
      return remote;
    } finally {
      requests.current.delete(abort);
      pending.current = false;
    }
  }
  function published(template: Template, revision: number) {
    adopt({
      draft: template,
      revision,
      saved: JSON.stringify(template),
      updatedAt: new Date().toISOString(),
    });
  }
  function readSaved() {
    // The caller confirms replacement before discarding unsaved edits.
    drafts.delete(key);
    setReload((value) => value + 1);
  }
  return {
    draft: state.draft,
    revision: state.revision,
    updatedAt: state.updatedAt,
    dirty,
    loading,
    loadError,
    update,
    save,
    published,
    readSaved,
    retryLoad: () => setReload((value) => value + 1),
  };
}
