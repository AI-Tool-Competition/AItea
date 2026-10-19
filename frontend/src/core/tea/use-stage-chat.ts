"use client";

import { useEffect, useRef, useState } from "react";

import { errorMessage, streamMessage, teaRequest } from "./api";
import { clearNodeAnalyses } from "./node-analysis";
import { mergeTraceStep, settleTraceSteps } from "./trace";
import type {
  CaseDetail,
  ChatSkill,
  ListResult,
  Message,
  MessageInput,
  TraceStep,
} from "./types";

export interface ChatTurn {
  request: MessageInput;
  afterMessageId?: string;
  text: string;
  label: string;
  state: "sending" | "failed" | "stopped";
  error?: string;
  steps?: TraceStep[];
}
type Draft = { text: string; turns: ChatTurn[] };
type ChatEntry =
  | { message: Message; turn?: never }
  | { turn: ChatTurn; message?: never };
const drafts = new Map<string, Draft>();
const controllers = new Set<AbortController>();
export function clearChatDrafts() {
  clearNodeAnalyses();
  for (const controller of controllers) controller.abort();
  controllers.clear();
  drafts.clear();
}

export function useStageChat({
  accountId,
  caseId,
  stageId,
  conversationId,
  mode = "auto",
  skills = [],
  onUpdate,
}: {
  accountId: string;
  caseId: string;
  stageId: string;
  conversationId?: string;
  mode?: "auto" | "legal";
  skills?: ChatSkill[];
  onUpdate: (record: CaseDetail) => void;
}) {
  const scope = `${accountId}:${caseId}:${stageId}${conversationId ? ":" + conversationId : ""}`;
  const query = `stage_id=${encodeURIComponent(stageId)}${conversationId ? "&conversation_id=" + encodeURIComponent(conversationId) : ""}`;
  const [draft] = useState<Draft>(() => {
    const value = drafts.get(scope) ?? { text: "", turns: [] };
    drafts.set(scope, value);
    return value;
  });
  const [text, setText] = useState(draft.text);
  const [history, setHistory] = useState<ListResult<Message>>({
    items: [],
    total: 0,
  });
  const [turns, setTurnState] = useState<ChatTurn[]>(draft.turns);
  const [loading, setLoading] = useState(true);
  const [moreBusy, setMoreBusy] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [reload, setReload] = useState(0);
  const active = useRef<
    { controller: AbortController; turn: ChatTurn } | undefined
  >(undefined);
  const mounted = useRef(false);
  const latestUpdate = useRef(onUpdate);
  latestUpdate.current = onUpdate;
  const moreAbort = useRef<AbortController | undefined>(undefined);

  function setTurns(update: (current: ChatTurn[]) => ChatTurn[]) {
    const next = update(draft.turns);
    if (drafts.get(scope) === draft) draft.turns = next;
    setTurnState(next);
  }
  function edit(value: string) {
    draft.text = value;
    setText(value);
  }
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      const sending = active.current;
      if (sending && drafts.get(scope) === draft)
        draft.turns = draft.turns.map((turn) =>
          turn.request.client_message_id ===
          sending.turn.request.client_message_id
            ? {
                ...sending.turn,
                state: "stopped",
                label: "上次回复未完成",
                steps: settleTraceSteps(sending.turn.steps ?? [], "cancelled"),
              }
            : turn,
        );
      sending?.controller.abort();
      moreAbort.current?.abort();
    };
  }, [scope, draft]);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setHistoryError("");
    teaRequest<ListResult<Message>>(
      `/cases/${encodeURIComponent(caseId)}/messages?${query}`,
      { signal: abort.signal },
    )
      .then((result) => {
        if (!abort.signal.aborted) setHistory(result);
      })
      .catch((error) => {
        if (!abort.signal.aborted) setHistoryError(errorMessage(error));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [caseId, stageId, query, reload]);
  useEffect(() => {
    if (!text && !turns.length) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [text, turns]);

  useEffect(() => {
    if (!conversationId) return;
    const abort = new AbortController();
    let reading = false;
    const refresh = async () => {
      if (reading || active.current || document.visibilityState === "hidden")
        return;
      reading = true;
      try {
        const result = await teaRequest<ListResult<Message>>(
          `/cases/${encodeURIComponent(caseId)}/messages?${query}`,
          { signal: abort.signal },
        );
        if (!abort.signal.aborted && mounted.current) {
          setHistory((previous) => ({
            items: [
              ...new Map(
                [...previous.items, ...result.items].map((item) => [
                  item.id,
                  item,
                ]),
              ).values(),
            ].sort(
              (a, b) =>
                a.created_at.localeCompare(b.created_at) ||
                a.id.localeCompare(b.id),
            ),
            total: result.total,
          }));
          setHistoryError("");
        }
      } catch {
        /* Keep the current conversation visible until an explicit retry or focus refresh. */
      } finally {
        reading = false;
      }
    };
    const timer = setInterval(() => {
      void refresh();
    }, 5000);
    const onFocus = () => {
      void refresh();
    };
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      abort.abort();
    };
  }, [caseId, conversationId, query]);

  async function submit(request: MessageInput) {
    if (active.current || loading || historyError) return;
    const controller = new AbortController();
    const turn: ChatTurn = {
      request,
      afterMessageId: history.items.at(-1)?.id,
      text: "",
      label: "正在发送…",
      state: "sending",
    };
    active.current = { controller, turn };
    controllers.add(controller);
    setTurns((current) => [
      ...current.filter(
        (item) => item.request.client_message_id !== request.client_message_id,
      ),
      turn,
    ]);
    let frame: ReturnType<typeof setTimeout> | undefined;
    const current = () =>
      mounted.current &&
      !controller.signal.aborted &&
      drafts.get(scope) === draft;
    const publish = () => {
      frame = undefined;
      if (current())
        setTurns((items) =>
          items.map((item) =>
            item.request.client_message_id === request.client_message_id
              ? { ...turn }
              : item,
          ),
        );
    };
    try {
      const result = await streamMessage(caseId, request, {
        signal: controller.signal,
        onEvent: (event) => {
          if (!current()) return;
          if (event.event === "start") turn.text = "";
          else if (event.event === "delta") turn.text += event.data.text;
          else if (event.event === "step")
            turn.steps = mergeTraceStep(turn.steps ?? [], event.data);
          else turn.label = event.data.label;
          frame ??= setTimeout(publish, 16);
        },
      });
      if (!current()) return;
      setHistory((previous) => {
        const messages = new Map(previous.items.map((item) => [item.id, item]));
        const added = result.messages.filter(
          (item) => !messages.has(item.id),
        ).length;
        for (const message of result.messages)
          messages.set(message.id, message);
        return { items: [...messages.values()], total: previous.total + added };
      });
      setTurns((items) =>
        items.filter(
          (item) =>
            item.request.client_message_id !== request.client_message_id,
        ),
      );
      latestUpdate.current(result.case);
    } catch (error) {
      if (!mounted.current || drafts.get(scope) !== draft) return;
      turn.state = controller.signal.aborted ? "stopped" : "failed";
      turn.label = controller.signal.aborted ? "已停止回复" : "回复未完成";
      turn.error = controller.signal.aborted ? undefined : errorMessage(error);
      turn.steps = settleTraceSteps(
        turn.steps ?? [],
        controller.signal.aborted ? "cancelled" : "failed",
      );
      setTurns((items) =>
        items.map((item) =>
          item.request.client_message_id === request.client_message_id
            ? { ...turn }
            : item,
        ),
      );
    } finally {
      clearTimeout(frame);
      controllers.delete(controller);
      if (active.current?.controller === controller) active.current = undefined;
    }
  }
  function send(taskId: string, suppliedContent?: string) {
    const content = (suppliedContent ?? text).trim();
    if (!content || active.current || loading || historyError) return;
    edit("");
    void submit({
      stage_id: stageId,
      ...(taskId ? { task_id: taskId } : {}),
      ...(conversationId
        ? { conversation_id: conversationId, mode, skills }
        : {}),
      content,
      client_message_id: crypto.randomUUID(),
    });
  }
  async function loadEarlier() {
    if (moreBusy || active.current) return;
    const abort = new AbortController();
    moreAbort.current = abort;
    setMoreBusy(true);
    try {
      const result = await teaRequest<ListResult<Message>>(
        `/cases/${encodeURIComponent(caseId)}/messages?${query}&limit=100&offset=${history.items.length}`,
        { signal: abort.signal },
      );
      if (!abort.signal.aborted)
        setHistory((previous) => ({
          items: [
            ...new Map(
              [...result.items, ...previous.items].map((message) => [
                message.id,
                message,
              ]),
            ).values(),
          ],
          total: Math.max(previous.total, result.total),
        }));
    } catch (error) {
      if (!abort.signal.aborted) setHistoryError(errorMessage(error));
    } finally {
      if (!abort.signal.aborted) setMoreBusy(false);
    }
  }
  // Pending turns stay where they were sent even if a later turn completes.
  // Missing anchors belong before the currently loaded history page.
  const savedIds = new Set(history.items.map((message) => message.id));
  const timeline: ChatEntry[] = turns
    .filter(
      (turn) => !turn.afterMessageId || !savedIds.has(turn.afterMessageId),
    )
    .map((turn) => ({ turn }));
  for (const message of history.items) {
    timeline.push({ message });
    for (const turn of turns)
      if (turn.afterMessageId === message.id) timeline.push({ turn });
  }
  return {
    text,
    edit,
    messages: history.items,
    hasEarlier: history.items.length < history.total,
    turns,
    timeline,
    sending: turns.some((turn) => turn.state === "sending"),
    loading,
    moreBusy,
    historyError,
    send,
    retry: (turn: ChatTurn) => {
      void submit(turn.request);
    },
    stop: () => active.current?.controller.abort(),
    loadEarlier,
    reload: () => setReload((value) => value + 1),
  };
}
