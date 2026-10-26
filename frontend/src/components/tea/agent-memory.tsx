"use client";

import { Brain, Pencil, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { errorMessage, teaRequest } from "@/core/tea/api";
import {
  getMemoryDraft,
  saveMemoryDraft,
  type MemoryDraft,
} from "@/core/tea/memory-drafts";
import type { AgentMemoryEntry, ListResult } from "@/core/tea/types";

export function AgentMemory({
  accountId,
  caseId,
  stageId,
  conversationId,
}: {
  accountId: string;
  caseId: string;
  stageId: string;
  conversationId?: string;
}) {
  const scope = `${accountId}:${caseId}:${stageId}:${conversationId ?? ""}`;
  const base = `/cases/${caseId}/stages/${stageId}/memories`;
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<MemoryDraft>(() => getMemoryDraft(scope));
  const [list, setList] = useState<ListResult<AgentMemoryEntry>>({
    items: [],
    total: 0,
  });
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const refreshRevision = useRef(false);
  const activeScope = useRef(scope);
  activeScope.current = scope;
  useEffect(() => {
    setDraft(getMemoryDraft(scope));
    setList({ items: [], total: 0 });
    refreshRevision.current = false;
    return () => {
      controller.current?.abort();
    };
  }, [scope]);
  const changeDraft = (value: MemoryDraft) => {
    setDraft(value);
    saveMemoryDraft(scope, value);
  };

  useEffect(() => {
    if (!open) return;
    const request = new AbortController();
    setLoading(true);
    setError("");
    const query = new URLSearchParams({ limit: "50" });
    if (conversationId) query.set("conversation_id", conversationId);
    void teaRequest<ListResult<AgentMemoryEntry>>(`${base}?${query}`, {
      signal: request.signal,
    })
      .then((value) => {
        if (request.signal.aborted) return;
        setList(value);
        if (refreshRevision.current) {
          refreshRevision.current = false;
          setDraft((previous) => {
            if (!previous.editing) return previous;
            const current = value.items.find(
              (item) => item.id === previous.editing?.id,
            );
            if (!current) return previous;
            // Keep the user's text; an explicit refresh exposes the new saved
            // version below before the user chooses whether to save again.
            const next = { ...previous, editing: current };
            saveMemoryDraft(scope, next);
            return next;
          });
        }
      })
      .catch((reason: unknown) => {
        if (!request.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!request.signal.aborted) setLoading(false);
      });
    return () => request.abort();
  }, [base, conversationId, open, refresh, scope]);

  async function mutate(path: string, options: RequestInit, clear = false) {
    const request = new AbortController();
    controller.current?.abort();
    controller.current = request;
    setBusy(true);
    setError("");
    try {
      await teaRequest(path, { ...options, signal: request.signal });
      if (request.signal.aborted || activeScope.current !== scope) return;
      if (clear) changeDraft({ content: "", kind: "note", editing: null });
      setRefresh((value) => value + 1);
    } catch (reason) {
      if (!request.signal.aborted && activeScope.current === scope)
        setError(errorMessage(reason));
    } finally {
      if (!request.signal.aborted && activeScope.current === scope)
        setBusy(false);
    }
  }

  async function more() {
    const request = new AbortController();
    controller.current?.abort();
    controller.current = request;
    setBusy(true);
    setError("");
    const query = new URLSearchParams({
      limit: "50",
      offset: String(list.items.length),
    });
    if (conversationId) query.set("conversation_id", conversationId);
    try {
      const value = await teaRequest<ListResult<AgentMemoryEntry>>(
        `${base}?${query}`,
        { signal: request.signal },
      );
      if (!request.signal.aborted && activeScope.current === scope)
        setList((previous) => ({
          items: [
            ...previous.items,
            ...value.items.filter(
              (item) => !previous.items.some((old) => old.id === item.id),
            ),
          ],
          total: value.total,
        }));
    } catch (reason) {
      if (!request.signal.aborted && activeScope.current === scope)
        setError(errorMessage(reason));
    } finally {
      if (!request.signal.aborted && activeScope.current === scope)
        setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" size="sm" variant="ghost">
          <Brain size={14} />
          记忆
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>当前节点的记忆</DialogTitle>
          <DialogDescription>
            保存偏好或业务笔记，供后续对话参考。记忆不替代法规证据或任务核验；删除记忆不会删除聊天记录。
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            void mutate(
              draft.editing ? `${base}/${draft.editing.id}` : base,
              {
                method: draft.editing ? "PATCH" : "POST",
                body: JSON.stringify({
                  content: draft.content.trim(),
                  kind: draft.kind,
                  conversation_id:
                    draft.editing?.conversation_id ?? conversationId ?? null,
                  expected_revision: draft.editing?.revision ?? null,
                }),
              },
              true,
            );
          }}
        >
          <label className="block text-sm" htmlFor="tea-memory-content">
            {draft.editing ? "编辑记忆" : "新增记忆"}
          </label>
          <textarea
            id="tea-memory-content"
            className="border-input w-full resize-y rounded-md border p-3 text-sm"
            rows={3}
            maxLength={2000}
            readOnly={busy}
            value={draft.content}
            onChange={(event) =>
              changeDraft({ ...draft, content: event.target.value })
            }
            placeholder="例如：回复先给材料清单，再解释原因"
          />
          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="记忆类型"
              className="border-input rounded-md border p-2 text-sm"
              value={draft.kind}
              disabled={busy}
              onChange={(event) =>
                changeDraft({
                  ...draft,
                  kind: event.target.value as AgentMemoryEntry["kind"],
                })
              }
            >
              <option value="note">业务笔记</option>
              <option value="preference">回复偏好</option>
            </select>
            <Button
              size="sm"
              disabled={busy || !draft.content.trim()}
              type="submit"
            >
              {busy ? "正在保存…" : "保存记忆"}
            </Button>
            {draft.editing && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={busy}
                onClick={() =>
                  changeDraft({ content: "", kind: "note", editing: null })
                }
              >
                取消编辑
              </Button>
            )}
          </div>
        </form>
        {error && (
          <div role="alert" className="text-destructive text-sm">
            {error}
            <Button
              type="button"
              variant="link"
              onClick={() => {
                refreshRevision.current = true;
                setRefresh((value) => value + 1);
              }}
            >
              刷新列表
            </Button>
          </div>
        )}
        {loading ? (
          <p role="status" className="text-muted-foreground text-sm">
            正在读取记忆…
          </p>
        ) : !list.items.length ? (
          <p className="text-muted-foreground text-sm">
            还没有保存的记忆。可以在这里新增，或在对话中明确说“请记住”。
          </p>
        ) : (
          <ul className="space-y-3">
            {list.items.map((item) => (
              <li className="rounded-lg border p-3" key={item.id}>
                <p className="text-sm break-words whitespace-pre-wrap">
                  {item.content}
                </p>
                <div className="text-muted-foreground mt-2 flex flex-wrap items-center gap-2 text-xs">
                  <span>
                    {item.kind === "preference" ? "回复偏好" : "业务笔记"}
                  </span>
                  <span>{item.conversation_id ? "本会话" : "节点共享"}</span>
                  {item.stale && <span>业务资料已变化，待核对</span>}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`编辑记忆：${item.content.slice(0, 20)}`}
                    disabled={
                      busy ||
                      Boolean(draft.content && draft.editing?.id !== item.id)
                    }
                    onClick={() =>
                      changeDraft({
                        content: item.content,
                        kind: item.kind,
                        editing: item,
                      })
                    }
                  >
                    <Pencil size={14} />
                    编辑
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`删除记忆：${item.content.slice(0, 20)}`}
                    disabled={busy}
                    onClick={() =>
                      void mutate(
                        `${base}/${item.id}?expected_revision=${item.revision}`,
                        { method: "DELETE" },
                        draft.editing?.id === item.id,
                      )
                    }
                  >
                    <Trash2 size={14} />
                    删除
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
        {list.items.length < list.total && (
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            onClick={() => void more()}
          >
            加载更多记忆
          </Button>
        )}
      </DialogContent>
    </Dialog>
  );
}
