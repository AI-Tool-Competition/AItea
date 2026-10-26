"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

import { errorMessage, streamMessage, teaRequest, TeaApiError } from "./api";
import {
  cacheChatHistory,
  clearChatHistory,
  mergeChatHistory,
  readChatHistory,
} from "./chat-cache";
import { clearNodeAnalyses } from "./node-analysis";
import { createStreamPublisher } from "./stream-publisher";
import { mergeToolDelta, mergeTraceStep, settleTraceSteps } from "./trace";
import type {
  CaseDetail,
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
  errorCode?: string;
  steps?: TraceStep[];
}
type Draft = { text: string; turns: ChatTurn[] };
type ChatEntry =
  | { message: Message; turn?: never }
  | { turn: ChatTurn; message?: never };
const drafts = new Map<string, Draft>();
const controllers = new Set<AbortController>();
export function clearChatDrafts() {
  clearChatHistory();
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
  onUpdate,
}: {
  accountId: string;
  caseId: string;
  stageId: string;
  conversationId?: string;
  onUpdate: (record: CaseDetail) => void;
}) {
  const scope = JSON.stringify([
    accountId,
    caseId,
    stageId,
    conversationId ?? null,
  ]);
  const query = `stage_id=${encodeURIComponent(stageId)}${conversationId ? "&conversation_id=" + encodeURIComponent(conversationId) : ""}`;
  const [draft] = useState<Draft>(() => {
    const value = drafts.get(scope) ?? { text: "", turns: [] };
    drafts.set(scope, value);
    return value;
  });
  const [text, setText] = useState(draft.text);
  const [cached] = useState(() => readChatHistory(scope));
  const [history, setHistory] = useState<ListResult<Message>>(
    cached ?? { items: [], total: 0 },
  );
  const historyRef = useRef(history);
  // Reconcile the saved pair with the optimistic articles without remounting
  // the streaming answer or interrupting the progress collapse animation.
  const messageKeys = useRef(new Map<string, string>());
  const historyReading = useRef(false);
  const [turns, setTurnState] = useState<ChatTurn[]>(draft.turns);
  const [loading, setLoading] = useState(!cached);
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

  const updateHistory = useCallback(
    (update: (previous: ListResult<Message>) => ListResult<Message>) => {
      if (!mounted.current || drafts.get(scope) !== draft) return;
      const next = update(historyRef.current);
      historyRef.current = next;
      cacheChatHistory(scope, next);
      setHistory(next);
    },
    [scope, draft],
  );

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
    controllers.add(abort);
    historyReading.current = true;
    setLoading(!cached);
    setHistoryError("");
    teaRequest<ListResult<Message>>(
      `/cases/${encodeURIComponent(caseId)}/messages?${query}`,
      { signal: abort.signal },
    )
      .then((result) => {
        if (!abort.signal.aborted)
          updateHistory((previous) => mergeChatHistory(previous, result));
      })
      .catch((error) => {
        if (!abort.signal.aborted && drafts.get(scope) === draft)
          setHistoryError(errorMessage(error));
      })
      .finally(() => {
        controllers.delete(abort);
        if (!abort.signal.aborted) {
          historyReading.current = false;
          setLoading(false);
        }
      });
    return () => {
      abort.abort();
      controllers.delete(abort);
      historyReading.current = false;
    };
  }, [caseId, stageId, query, reload, cached, draft, scope, updateHistory]);
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
    controllers.add(abort);
    let reading = false;
    const refresh = async () => {
      if (
        reading ||
        historyReading.current ||
        active.current ||
        document.visibilityState === "hidden"
      )
        return;
      reading = true;
      try {
        const result = await teaRequest<ListResult<Message>>(
          `/cases/${encodeURIComponent(caseId)}/messages?${query}`,
          { signal: abort.signal },
        );
        if (
          !abort.signal.aborted &&
          mounted.current &&
          drafts.get(scope) === draft
        ) {
          updateHistory((previous) => mergeChatHistory(previous, result));
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
      controllers.delete(abort);
    };
  }, [caseId, conversationId, query, draft, scope, updateHistory]);

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
    let firstTextPublished = false;
    const current = () =>
      mounted.current &&
      !controller.signal.aborted &&
      drafts.get(scope) === draft;
    const publisher = createStreamPublisher(() => {
      if (current())
        setTurns((items) =>
          items.map((item) =>
            item.request.client_message_id === request.client_message_id
              ? { ...turn }
              : item,
          ),
        );
    });
    try {
      const result = await streamMessage(caseId, request, {
        signal: controller.signal,
        onEvent: (event) => {
          if (!current()) return;
          if (event.event === "start") {
            if (!event.data.append) turn.text = "";
          } else if (event.event === "delta") turn.text += event.data.text;
          else if (event.event === "tool_delta")
            turn.steps = mergeToolDelta(turn.steps ?? [], event.data);
          else if (event.event === "step")
            turn.steps = mergeTraceStep(turn.steps ?? [], event.data);
          else turn.label = event.data.label;
          if (
            (event.event === "delta" || event.event === "tool_delta") &&
            event.data.text &&
            !firstTextPublished
          ) {
            firstTextPublished = true;
            // The first visible text must not wait behind token batching. Keep
            // subsequent updates frame-bound so input remains responsive.
            if (document.visibilityState === "visible")
              flushSync(publisher.flush);
            else publisher.flush();
          } else if (event.event === "delta" || event.event === "tool_delta")
            publisher.schedule();
          else publisher.flush();
        },
      });
      if (!current()) return;
      for (const message of result.messages)
        messageKeys.current.set(
          message.id,
          `${request.client_message_id}:${message.role}`,
        );
      updateHistory((previous) => {
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
      turn.errorCode = error instanceof TeaApiError ? error.code : undefined;
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
      publisher.cancel();
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
      ...(conversationId ? { conversation_id: conversationId } : {}),
      content,
      client_message_id: crypto.randomUUID(),
    });
  }
  async function loadEarlier() {
    if (moreBusy || active.current) return;
    const abort = new AbortController();
    controllers.add(abort);
    moreAbort.current = abort;
    setMoreBusy(true);
    try {
      const result = await teaRequest<ListResult<Message>>(
        `/cases/${encodeURIComponent(caseId)}/messages?${query}&limit=100&offset=${history.items.length}`,
        { signal: abort.signal },
      );
      if (!abort.signal.aborted)
        updateHistory((previous) => ({
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
      controllers.delete(abort);
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
    messageKey: (message: Message) =>
      messageKeys.current.get(message.id) ?? message.id,
    hasEarlier: history.items.length < history.total,
    turns,
    timeline,
    sending: turns.some((turn) => turn.state === "sending"),
    loading,
    moreBusy,
    historyError,
    send,
    retry: (turn: ChatTurn) => {
      if (
        [
          "agent_request_not_replayable",
          "agent_context_changed",
          "analysis_context_changed",
          "agent_lease_lost",
        ].includes(turn.errorCode ?? "")
      ) {
        setTurns((items) =>
          items.filter(
            (item) =>
              item.request.client_message_id !== turn.request.client_message_id,
          ),
        );
        void submit({
          ...turn.request,
          client_message_id: crypto.randomUUID(),
        });
      } else void submit(turn.request);
    },
    stop: () => active.current?.controller.abort(),
    loadEarlier,
    reload: () => setReload((value) => value + 1),
  };
}
