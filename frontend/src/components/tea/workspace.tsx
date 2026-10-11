"use client";
import {
  ArrowRight,
  ArrowUp,
  BookOpen,
  Check,
  CheckCheck,
  ChevronRight,
  Circle,
  FolderOpen,
  Leaf,
  LockKeyhole,
  PanelRightClose,
  PanelRightOpen,
  Paperclip,
  ShieldCheck,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { usePanelRef } from "react-resizable-panels";

import {
  Conversation,
  ConversationContent,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { MessageResponse } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { errorMessage, post, teaRequest } from "@/core/tea/api";
import {
  type Artifact,
  type Capabilities,
  type CaseDetail,
  type ListResult,
  type Message,
  type StageState,
} from "@/core/tea/types";
import { useIsMobile } from "@/hooks/use-mobile";

import { ArtifactPanel } from "./artifact-panel";
import { CitationCard } from "./library";
import { Busy, dateLabel, ErrorNotice, taskStatus, Trace } from "./shared";
import { TaskPanel } from "./task-panel";
type PanelTab = "flow" | "task" | "document" | "facts";

function Flow({
  record,
  stageId,
  onStage,
  onTask,
}: {
  record: CaseDetail;
  stageId: string;
  onStage: (stage: string) => void;
  onTask: (stage: string, task: string) => void;
}) {
  const percent = record.total_tasks
    ? Math.round((record.completed_tasks / record.total_tasks) * 100)
    : 0;
  return (
    <div className="tea-flow">
      <div className="tea-flow-intro">
        <div>
          <h3>办理进度</h3>
        </div>
        <span className="tea-progress-value">
          {percent}
          <small>%</small>
        </span>
      </div>
      <div
        className="tea-progress-track"
        role="progressbar"
        aria-label="业务完成进度"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <span style={{ transform: `scaleX(${percent / 100})` }} />
      </div>
      <div className="tea-flow-count">
        {record.completed_tasks} / {record.total_tasks} 项任务完成
        <span>模板 v{record.template_version}</span>
      </div>
      <div className="tea-flow-stages">
        {record.stages.map((stage, i) => (
          <section
            key={stage.id}
            className={`tea-flow-stage ${stage.status} ${stageId === stage.id ? "selected" : ""}`}
          >
            <div className="tea-flow-rail">
              <span>
                {stage.status === "completed" ? (
                  <Check size={15} />
                ) : stage.status === "locked" ? (
                  <LockKeyhole size={12} />
                ) : (
                  String(i + 1).padStart(2, "0")
                )}
              </span>
            </div>
            <div className="tea-stage-body">
              <button
                className="tea-stage-title"
                onClick={() => onStage(stage.id)}
              >
                <div>
                  <small>面向{stage.actor}</small>
                  <h4>{stage.name}</h4>
                </div>
                <span>
                  {stage.status === "active"
                    ? "进行中"
                    : stage.status === "completed"
                      ? "已完成"
                      : "待解锁"}
                </span>
              </button>
              <div className="tea-flow-tasks">
                {stage.tasks.map((task) => (
                  <button
                    key={task.id}
                    onClick={() => onTask(stage.id, task.id)}
                  >
                    {task.status === "completed" && !task.needs_review ? (
                      <CheckCheck size={15} />
                    ) : (
                      <span className={`tea-task-dot ${task.status}`} />
                    )}
                    <span>{task.title}</span>
                    {task.needs_review && <small>复核</small>}
                    <ChevronRight size={12} />
                  </button>
                ))}
              </div>
              <p>
                {stage.status === "locked"
                  ? "前一阶段完成后解锁"
                  : "本阶段任务可并行办理"}
              </p>
            </div>
          </section>
        ))}
      </div>
      <div className="tea-flow-footnote">
        <ShieldCheck size={16} />
        <span>
          逐项核验后推进
          <br />
          <small>办理记录与依据始终保留</small>
        </span>
      </div>
    </div>
  );
}

type ChatDraft = {
  text: string;
  key?: { content: string; task: string; id: string };
};
const chatDrafts = new Map<string, ChatDraft>();
export function clearChatDrafts() {
  chatDrafts.clear();
}

function StageChat({
  record,
  stage,
  taskId,
  onTask,
  onUpdate,
  onUpload,
  capabilities,
}: {
  record: CaseDetail;
  stage: StageState;
  taskId: string;
  onTask: (id: string) => void;
  onUpdate: (record: CaseDetail) => void;
  onUpload: () => void;
  capabilities: Capabilities;
}) {
  const cacheKey = `${record.id}:${stage.id}`;
  const [messages, setMessages] = useState<Message[]>([]);
  const [messageTotal, setMessageTotal] = useState(0);
  const [moreBusy, setMoreBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [text, setText] = useState(chatDrafts.get(cacheKey)?.text ?? "");
  const [sending, setSending] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const submitting = useRef(false);
  const alive = useRef(true);
  const input = useRef<HTMLTextAreaElement>(null);
  const messageKey = useRef<
    { content: string; task: string; id: string } | undefined
  >(chatDrafts.get(cacheKey)?.key);
  useEffect(() => {
    if (text) chatDrafts.set(cacheKey, { text, key: messageKey.current });
    else chatDrafts.delete(cacheKey);
  }, [cacheKey, text]);
  useEffect(() => {
    alive.current = true;
    const abort = new AbortController();
    setLoading(true);
    teaRequest<ListResult<Message>>(
      `/cases/${record.id}/messages?stage_id=${encodeURIComponent(stage.id)}`,
      { signal: abort.signal },
    )
      .then((result) => {
        setMessages(result.items);
        setMessageTotal(result.total);
        setError("");
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => {
      abort.abort();
      alive.current = false;
    };
  }, [record.id, stage.id, retryKey]);
  async function send() {
    const content = text.trim();
    if (!content || submitting.current) return;
    submitting.current = true;
    setSending(true);
    setError("");
    if (
      messageKey.current?.content !== content ||
      messageKey.current.task !== taskId
    )
      messageKey.current = { content, task: taskId, id: crypto.randomUUID() };
    chatDrafts.set(cacheKey, { text, key: messageKey.current });
    try {
      const response = await post<{ messages: Message[]; case: CaseDetail }>(
        `/cases/${record.id}/messages`,
        {
          stage_id: stage.id,
          ...(taskId ? { task_id: taskId } : {}),
          content,
          client_message_id: messageKey.current.id,
        },
      );
      onUpdate(response.case);
      if (chatDrafts.get(cacheKey)?.key?.id === messageKey.current?.id)
        chatDrafts.delete(cacheKey);
      if (alive.current) {
        setMessages((current) => [
          ...new Map(
            [...current, ...response.messages].map((message) => [
              message.id,
              message,
            ]),
          ).values(),
        ]);
        setText("");
        chatDrafts.delete(cacheKey);
        setMessageTotal(
          (total) =>
            total +
            response.messages.filter(
              (message) =>
                !messages.some((existing) => existing.id === message.id),
            ).length,
        );
        messageKey.current = undefined;
        input.current?.focus();
      }
    } catch (e) {
      if (alive.current) setError(errorMessage(e));
    } finally {
      submitting.current = false;
      if (alive.current) setSending(false);
    }
  }
  async function loadEarlier() {
    if (moreBusy) return;
    setMoreBusy(true);
    try {
      const result = await teaRequest<ListResult<Message>>(
        `/cases/${record.id}/messages?stage_id=${encodeURIComponent(stage.id)}&limit=100&offset=${messages.length}`,
      );
      if (alive.current) {
        setMessages((current) => [
          ...new Map(
            [...result.items, ...current].map((message) => [
              message.id,
              message,
            ]),
          ).values(),
        ]);
        setMessageTotal(result.total);
      }
    } catch (e) {
      if (alive.current) setError(errorMessage(e));
    } finally {
      if (alive.current) setMoreBusy(false);
    }
  }
  const focusedTask = stage.tasks.find((t) => t.id === taskId);
  return (
    <div className="tea-chat">
      <Conversation
        key={stage.id}
        initial="instant"
        resize="instant"
        className="tea-conversation"
      >
        <ConversationContent className="tea-conversation-content">
          <div className="tea-stage-intro">
            <h1>{stage.name}</h1>
            <p>{stage.description}</p>
            <div className="tea-stage-route">
              {record.template.origin} <ArrowRight size={12} />{" "}
              {record.template.destination} <span> / </span> 面向{stage.actor}
            </div>
            <div className="tea-stage-context">
              <span>
                <FolderOpen size={13} />
                {record.company}
              </span>
              <span>
                <Leaf size={13} />
                {record.product}
              </span>
            </div>
          </div>
          <div className="tea-parallel-tasks">
            <div className="tea-list-label">
              <span>本阶段任务</span>
              <span>
                {stage.tasks.filter((t) => t.status === "completed").length}/
                {stage.tasks.length} 完成 · 可并行
              </span>
            </div>
            {stage.tasks.map((task, i) => (
              <button
                key={task.id}
                onClick={() => onTask(task.id)}
                className={`tea-inline-task ${taskId === task.id ? "selected" : ""}`}
              >
                <span className={`tea-inline-task-icon ${task.status}`}>
                  {task.status === "completed" ? (
                    <Check size={16} />
                  ) : (
                    String(i + 1).padStart(2, "0")
                  )}
                </span>
                <span>
                  <strong>{task.title}</strong>
                  <small>
                    {task.needs_review ? "需要复核" : taskStatus[task.status]}
                    {task.require_document ? " · 需要材料" : ""}
                  </small>
                </span>
                <ChevronRight size={15} />
              </button>
            ))}
          </div>
          <div className="tea-chat-divider">
            <span />
            本节点对话
            <span />
          </div>
          {!loading && messages.length < messageTotal && (
            <Button
              variant="ghost"
              size="sm"
              disabled={moreBusy || sending}
              onClick={loadEarlier}
            >
              {moreBusy ? "正在读取…" : "加载更早对话"}
            </Button>
          )}
          {loading ? (
            <Busy label="读取对话记录…" />
          ) : messages.length ? (
            messages.map((message) => (
              <article
                key={message.id}
                className={`tea-message ${message.role}`}
              >
                <div className="tea-message-avatar">
                  {message.role === "assistant" ? (
                    <Leaf size={16} strokeWidth={1.6} />
                  ) : (
                    "我"
                  )}
                </div>
                <div className="tea-message-body">
                  <div className="tea-message-label">
                    {message.role === "assistant" ? "茶渡助手" : "你"}
                    <span>{dateLabel(message.created_at)}</span>
                  </div>
                  <div className="tea-message-content">
                    <MessageResponse>{message.content}</MessageResponse>
                  </div>
                  {message.trace?.length > 0 && <Trace steps={message.trace} />}
                  {message.citations?.length > 0 && (
                    <details className="tea-message-citations">
                      <summary>
                        <BookOpen size={13} />
                        查看 {message.citations.length} 项引用依据
                      </summary>
                      {message.citations.map((citation, i) => (
                        <CitationCard
                          key={`${citation.document_id}-${i}`}
                          citation={citation}
                        />
                      ))}
                    </details>
                  )}
                </div>
              </article>
            ))
          ) : (
            <article className="tea-message assistant">
              <div className="tea-message-avatar">
                <Leaf size={16} />
              </div>
              <div className="tea-message-body">
                <div className="tea-message-label">茶渡助手</div>
                <div className="tea-message-content">
                  {stage.status === "locked"
                    ? "这个阶段尚未解锁。你可以先了解任务要求，完成前一阶段后继续办理。"
                    : "我们一起把这一阶段准备好。上方任务可以并行办理；你可以先提问、上传资料，或打开任务填报信息并申请核验。"}
                </div>
              </div>
            </article>
          )}
          {sending && (
            <div className="tea-assistant-working" role="status">
              <Leaf size={15} />
              <span>正在读取业务资料与相关依据</span>
              <i />
              <i />
              <i />
            </div>
          )}
        </ConversationContent>
        <ConversationScrollButton
          aria-label="滚动到最新消息"
          className="tea-scroll-bottom"
        />
      </Conversation>
      {error && (
        <div className="tea-composer-error">
          <ErrorNotice
            message={error}
            onRetry={loading ? undefined : () => setRetryKey((k) => k + 1)}
          />
        </div>
      )}
      <div className="tea-composer-area">
        {focusedTask && (
          <div className="tea-current-task">
            <Circle size={10} />
            正在讨论：{focusedTask.title}
            <button onClick={() => onTask("")} aria-label="取消任务聚焦">
              <X size={12} />
            </button>
          </div>
        )}
        <form
          className="tea-composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <textarea
            ref={input}
            aria-label="向茶渡助手提问"
            value={text}
            maxLength={12000}
            placeholder={
              stage.status === "locked"
                ? "可以先咨询这一阶段的准备要求…"
                : "说说你的问题，或汇报最新的办理进度…"
            }
            onChange={(e) => setText(e.target.value)}
            disabled={sending}
            rows={2}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing &&
                e.keyCode !== 229
              ) {
                e.preventDefault();
                void send();
              }
            }}
          />
          <div className="tea-composer-toolbar">
            <button type="button" onClick={onUpload} title="上传资料">
              <Paperclip size={17} />
              <span>添加资料</span>
            </button>
            <span className="tea-composer-context">
              <ShieldCheck size={12} />
              共享本次业务档案
            </span>
            <button
              className="tea-send"
              type="submit"
              aria-label="发送消息"
              disabled={!text.trim() || sending || loading}
            >
              <ArrowUp size={17} />
            </button>
          </div>
        </form>
        <p className="tea-composer-note">
          {capabilities.assistant_mode === "demo"
            ? "当前为演示助手，依据已上传资料和配置规则提供指导。"
            : "结论请结合引用原文及实际办理要求核对。"}
          <span>Enter 发送 · Shift + Enter 换行</span>
        </p>
      </div>
    </div>
  );
}

export function Workspace({
  record,
  stageId,
  setStageId,
  capabilities,
  onUpdate,
  onUpload,
}: {
  record: CaseDetail;
  stageId: string;
  setStageId: (id: string) => void;
  capabilities: Capabilities;
  onUpdate: (record: CaseDetail) => void;
  onUpload: () => void;
}) {
  const [panelOpen, setPanelOpen] = useState(true);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [tab, setTab] = useState<PanelTab>("flow");
  const [taskId, setTaskId] = useState("");
  const [artifactId, setArtifactId] = useState("");
  const isMobile = useIsMobile();
  const panelRef = usePanelRef();
  const openPanelSize = useRef("40%");
  const stage = record.stages.find((s) => s.id === stageId) ?? record.stages[0];
  const task = stage?.tasks.find((t) => t.id === taskId);
  const selectedArtifact =
    record.artifacts.find((a) => a.id === artifactId) ?? record.artifacts[0];
  useEffect(() => {
    if (isMobile) return;
    if (panelOpen) panelRef.current?.resize(openPanelSize.current);
    else panelRef.current?.collapse();
  }, [panelOpen, isMobile, panelRef]);
  function openPanel(next: PanelTab) {
    setTab(next);
    setPanelOpen(true);
    if (isMobile) setMobileOpen(true);
  }
  function selectTask(id: string, targetStage?: string) {
    if (targetStage) setStageId(targetStage);
    setTaskId(id);
    if (id) openPanel("task");
  }
  async function updateArtifact(artifact: Artifact) {
    setArtifactId(artifact.id);
    const refreshed = await teaRequest<CaseDetail>(`/cases/${record.id}`);
    onUpdate(refreshed);
  }

  if (!stage)
    return (
      <div className="tea-page">
        <ErrorNotice message="业务模板没有可用节点，请检查配置。" />
      </div>
    );
  const panel = (
    <div className="tea-sidepanel">
      <div className="tea-sidepanel-header">
        <div className="tea-panel-tabs">
          {(
            [
              ["flow", "办理流程"],
              ["task", "当前任务"],
              ["document", "文稿"],
              ["facts", "档案"],
            ] as const
          ).map(([id, label]) => (
            <button
              className={tab === id ? "active" : ""}
              key={id}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          onClick={() => {
            setPanelOpen(false);
            setMobileOpen(false);
          }}
          aria-label="收起右侧面板"
        >
          <PanelRightClose size={17} />
        </button>
      </div>
      <div className="tea-sidepanel-scroll">
        <div hidden={tab !== "flow"}>
          <Flow
            record={record}
            stageId={stage.id}
            onStage={(id) => {
              setStageId(id);
              setTaskId("");
              if (isMobile) setMobileOpen(false);
            }}
            onTask={(s, t) => selectTask(t, s)}
          />
        </div>
        <div hidden={tab !== "task"}>
          {task ? (
            <TaskPanel
              key={task.id}
              task={task}
              record={record}
              locked={stage.status === "locked"}
              onUpdate={onUpdate}
              onUpload={onUpload}
              onArtifact={async (artifact) => {
                await updateArtifact(artifact);
                openPanel("document");
              }}
            />
          ) : (
            <div className="tea-empty">
              <ShieldCheck size={28} />
              <h3>选择一项任务</h3>
              <p>在流程或对话上方打开任务，填报信息并申请核验。</p>
              <Button variant="outline" onClick={() => setTab("flow")}>
                查看办理流程
              </Button>
            </div>
          )}
        </div>
        <div hidden={tab !== "document"}>
          {record.artifacts.length > 1 && (
            <label className="tea-artifact-picker">
              <span className="sr-only">选择文稿</span>
              <select
                value={selectedArtifact?.id ?? ""}
                onChange={(e) => {
                  setArtifactId(e.target.value);
                }}
              >
                {record.artifacts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.title}
                  </option>
                ))}
              </select>
            </label>
          )}
          <ArtifactPanel
            key={`${selectedArtifact?.id}-${selectedArtifact?.version}`}
            artifact={selectedArtifact}
            onSaved={updateArtifact}
          />
        </div>
        <div hidden={tab !== "facts"} className="tea-facts-panel">
          <div className="tea-panel-label">
            <FolderOpen size={17} />
            本次业务共享档案
          </div>
          <p className="tea-small-note">
            节点之间共享已记录的信息，关键事实保留来源。
          </p>
          {record.facts.length ? (
            record.facts.map((fact) => (
              <div className="tea-fact" key={fact.key}>
                <span>
                  {fact.label}
                  <small>{fact.confirmed ? "已确认" : "待确认"}</small>
                </span>
                <strong>{String(fact.value)}</strong>
                <p>来源：{fact.source}</p>
              </div>
            ))
          ) : (
            <p className="tea-small-note">
              完成任务填报后，业务事实会在这里汇总。
            </p>
          )}
          <div className="tea-panel-label tea-record-label">办理记录</div>
          {record.events
            .slice()
            .reverse()
            .map((event) => (
              <div className="tea-event" key={event.id}>
                <span />
                <div>
                  <strong>{event.title}</strong>
                  <p>{event.detail}</p>
                  <small>{dateLabel(event.created_at)}</small>
                </div>
              </div>
            ))}
        </div>
      </div>
    </div>
  );
  return (
    <div className="tea-workspace">
      <header className="tea-workspace-toolbar">
        <div className="tea-workspace-heading">
          <SidebarTrigger className="md:hidden" aria-label="展开导航" />
          <span className="tea-workspace-title">{record.title}</span>
          <span className="tea-workspace-stage">{stage.name}</span>
        </div>
        <div className="tea-workspace-actions">
          <span className="tea-mode-indicator">
            {capabilities.assistant_mode === "demo" ? "演示助手" : "AI 助手"}
          </span>
          <button
            aria-expanded={isMobile ? mobileOpen : panelOpen}
            onClick={() => {
              if (isMobile) setMobileOpen(true);
              else setPanelOpen(!panelOpen);
            }}
          >
            {panelOpen && !isMobile ? (
              <PanelRightClose size={16} />
            ) : (
              <PanelRightOpen size={16} />
            )}
            {panelOpen && !isMobile ? "收起流程" : "办理流程"}
          </button>
        </div>
      </header>
      {isMobile ? (
        <>
          <StageChat
            key={stage.id}
            record={record}
            stage={stage}
            taskId={taskId}
            onTask={selectTask}
            onUpdate={onUpdate}
            onUpload={onUpload}
            capabilities={capabilities}
          />
          <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
            <SheetContent className="tea-theme tea-mobile-panel">
              <SheetHeader className="sr-only">
                <SheetTitle>办理流程与资料</SheetTitle>
                <SheetDescription>
                  查看任务、核验进度和编辑文稿。
                </SheetDescription>
              </SheetHeader>
              {panel}
            </SheetContent>
          </Sheet>
        </>
      ) : (
        <ResizablePanelGroup
          orientation="horizontal"
          className="tea-workspace-panels"
        >
          <ResizablePanel id="conversation" minSize="30%" defaultSize="60%">
            <StageChat
              key={stage.id}
              record={record}
              stage={stage}
              taskId={taskId}
              onTask={selectTask}
              onUpdate={onUpdate}
              onUpload={onUpload}
              capabilities={capabilities}
            />
          </ResizablePanel>
          <ResizableHandle
            withHandle
            disabled={!panelOpen}
            className="tea-resize-handle"
          />
          <ResizablePanel
            id="tea-details"
            panelRef={panelRef}
            collapsible
            collapsedSize={0}
            minSize="20%"
            maxSize="70%"
            defaultSize="40%"
            onResize={(size) => {
              if (size.asPercentage > 0) {
                openPanelSize.current = `${size.asPercentage}%`;
              }
              setPanelOpen(size.asPercentage > 0);
            }}
          >
            <div className="tea-details-content" hidden={!panelOpen}>
              {panel}
            </div>
          </ResizablePanel>
        </ResizablePanelGroup>
      )}
    </div>
  );
}
