"use client";

import {
  ArrowLeft,
  CheckCheck,
  ChevronRight,
  FileText,
  LockKeyhole,
  PanelRightClose,
  PanelRightOpen,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { usePanelRef } from "react-resizable-panels";

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
import { mergeProcessingDocument } from "@/core/tea/document-state";
import type {
  Artifact,
  Capabilities,
  CaseDetail,
  Document,
  NodeConversation,
} from "@/core/tea/types";
import { useIsMobile } from "@/hooks/use-mobile";

import { ArtifactPanel } from "./artifact-panel";
import { Library } from "./library";
import { Busy, dateLabel, Empty, ErrorNotice } from "./shared";
import { StageChat } from "./stage-chat";
import { TaskPanel } from "./task-panel";
export { clearChatDrafts } from "@/core/tea/use-stage-chat";

type PanelTab = "task" | "library" | "document";
export function Workspace({
  accountId,
  record,
  stageId,
  conversationId = "guide",
  capabilities,
  onUpdate,
  onUpload,
  uploadedDocument,
}: {
  accountId: string;
  record: CaseDetail;
  stageId: string;
  setStageId: (id: string) => void;
  conversationId?: string;
  onConversation?: (id: string) => void;
  capabilities: Capabilities;
  onUpdate: (record: CaseDetail) => void;
  onUpload: () => void;
  uploadedDocument?: Document;
}) {
  const [panelOpen, setPanelOpen] = useState(true);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [narrow, setNarrow] = useState(false);
  const [tab, setTab] = useState<PanelTab>("task");
  const [taskId, setTaskId] = useState("");
  const [artifactId, setArtifactId] = useState("");
  const [guide, setGuide] = useState<{
    stageId: string;
    conversation: NodeConversation;
  }>();
  const [guideError, setGuideError] = useState<{
    stageId: string;
    message: string;
  }>();
  const [guideAttempt, setGuideAttempt] = useState(0);
  const isMobile = useIsMobile();
  const overlay = isMobile || narrow;
  const panelVisible = overlay ? mobileOpen : panelOpen;
  const lastUpload = useRef<Document | undefined>(undefined);
  useEffect(() => {
    if (
      uploadedDocument?.case_id !== record.id ||
      lastUpload.current === uploadedDocument
    )
      return;
    lastUpload.current = uploadedDocument;
    setTab("library");
    setMobileOpen(true);
    setPanelOpen(true);
  }, [uploadedDocument, record.id]);
  const panelRef = usePanelRef();
  const panelOpenRef = useRef(false);
  const previousDetailSize = useRef(0);
  const openPanelSize = useRef("360px");
  const stage =
    record.stages.find((item) => item.id === stageId) ?? record.stages[0];
  const task = stage?.tasks.find((item) => item.id === taskId);
  const artifact = record.artifacts.find((item) => item.id === artifactId);
  const isGuide = conversationId === "guide";
  const guideStageId = stage?.id;
  const guideStageStatus = stage?.status;
  const updateLibraryDocuments = useCallback(
    (updates: Document[]) => {
      const byId = new Map(updates.map((item) => [item.id, item]));
      let changed = false;
      const documents = record.documents.map((current) => {
        const next = byId.get(current.id);
        if (
          !next ||
          (current.status === next.status &&
            current.error === next.error &&
            current.chunk_count === next.chunk_count &&
            current.processing_detail?.state ===
              next.processing_detail?.state &&
            current.processing_detail?.phase ===
              next.processing_detail?.phase &&
            current.processing_detail?.retry_after_seconds ===
              next.processing_detail?.retry_after_seconds)
        )
          return current;
        changed = true;
        return mergeProcessingDocument(current, next);
      });
      if (changed) onUpdate({ ...record, documents });
    },
    [record, onUpdate],
  );
  useEffect(() => {
    const media = window.matchMedia("(max-width: 1100px)");
    const update = () => setNarrow(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    panelOpenRef.current = !overlay && panelOpen;
    if (!overlay && panelOpen) panelRef.current?.resize(openPanelSize.current);
    else panelRef.current?.collapse();
  }, [overlay, panelOpen, panelRef]);
  useEffect(() => {
    if (!guideStageId || !isGuide || guideStageStatus === "locked") return;
    const abort = new AbortController();
    setGuideError(undefined);
    post<NodeConversation>(
      `/cases/${encodeURIComponent(record.id)}/stages/${encodeURIComponent(guideStageId)}/guide-conversation`,
      {},
      abort.signal,
    )
      .then((value) => {
        if (!abort.signal.aborted)
          setGuide({ stageId: guideStageId, conversation: value });
      })
      .catch((reason) => {
        if (!abort.signal.aborted)
          setGuideError({
            stageId: guideStageId,
            message: errorMessage(reason),
          });
      });
    return () => abort.abort();
  }, [
    accountId,
    record.id,
    guideStageId,
    guideStageStatus,
    isGuide,
    guideAttempt,
  ]);
  function openPanel(next: PanelTab) {
    setTab(next);
    if (overlay) setMobileOpen(true);
    else setPanelOpen(true);
  }
  function selectTask(id: string) {
    setTaskId(id);
    if (id) openPanel("task");
  }
  async function updateArtifact(value: Artifact) {
    setArtifactId(value.id);
    const updated = await teaRequest<CaseDetail>(
      `/cases/${encodeURIComponent(record.id)}`,
    );
    onUpdate(updated);
  }
  if (!stage)
    return <ErrorNotice message="业务模板没有可用节点，请检查配置。" />;
  const currentConversation = isGuide
    ? guide?.stageId === stage.id
      ? guide.conversation.id
      : undefined
    : conversationId;
  const completed = stage.tasks.filter(
    (item) => item.status === "completed" && !item.needs_review,
  ).length;
  const panel = (
    <div className="tea-sidepanel">
      <div className="tea-sidepanel-header">
        <div className="tea-panel-tabs" role="tablist" aria-label="业务工作区">
          {(
            [
              ["task", "当前任务"],
              ["library", "资料库"],
              ["document", "文稿"],
            ] as const
          ).map(([id, label]) => (
            <button
              role="tab"
              aria-selected={tab === id}
              aria-controls={`workspace-${id}`}
              className={tab === id ? "active" : ""}
              key={id}
              onClick={() => setTab(id)}
            >
              {label}
              {id === "document" && record.artifacts.length > 0 && (
                <span className="tea-panel-count">
                  {record.artifacts.length}
                </span>
              )}
            </button>
          ))}
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={() => {
            setPanelOpen(false);
            setMobileOpen(false);
          }}
          aria-label="收起右侧面板"
        >
          <PanelRightClose size={17} />
        </Button>
      </div>
      <div className="tea-sidepanel-scroll">
        <div
          id="workspace-task"
          role="tabpanel"
          aria-label="当前任务"
          hidden={tab !== "task"}
        >
          {task ? (
            <>
              <Button
                className="tea-panel-back"
                variant="ghost"
                onClick={() => setTaskId("")}
              >
                <ArrowLeft size={14} />
                返回节点任务
              </Button>
              <TaskPanel
                key={task.id}
                task={task}
                record={record}
                locked={stage.status === "locked"}
                onUpdate={onUpdate}
                onUpload={onUpload}
                onArtifact={async (value) => {
                  await updateArtifact(value);
                  openPanel("document");
                }}
              />
            </>
          ) : (
            <div className="tea-current-node-tasks">
              <div className="tea-node-task-intro">
                <p>当前节点</p>
                <h3>{stage.name}</h3>
                <span>
                  {completed} / {stage.tasks.length} 项完成
                </span>
              </div>
              <div
                className="tea-progress-track"
                role="progressbar"
                aria-label="当前节点进度"
                aria-valuemin={0}
                aria-valuemax={stage.tasks.length}
                aria-valuenow={completed}
              >
                <span
                  style={{
                    transform: `scaleX(${stage.tasks.length ? completed / stage.tasks.length : 0})`,
                  }}
                />
              </div>
              <p className="tea-small-note">
                {stage.status === "locked"
                  ? "完成前序节点的必办任务后解锁。"
                  : "各会话共用这里的办理进度。任务通过核验后，引导会话会追加分析。"}
              </p>
              <div className="tea-node-task-list">
                {stage.tasks.map((item) => (
                  <button key={item.id} onClick={() => selectTask(item.id)}>
                    {item.status === "completed" && !item.needs_review ? (
                      <CheckCheck size={17} />
                    ) : (
                      <span className={`tea-task-dot ${item.status}`} />
                    )}
                    <span>
                      <strong>{item.title}</strong>
                      <small>
                        {item.needs_review
                          ? "需要复核"
                          : item.status === "completed"
                            ? "已完成"
                            : item.status === "needs_input"
                              ? "待补充"
                              : item.status === "in_progress"
                                ? "进行中"
                                : "待办理"}
                        {Object.values(item.values).some(
                          (value) => value !== "" && value != null,
                        ) && item.status !== "completed"
                          ? ` · 已填写 ${Object.values(item.values).filter((value) => value !== "" && value != null).length} 项，待核对`
                          : ""}
                      </small>
                    </span>
                    <ChevronRight size={14} />
                  </button>
                ))}
              </div>
              <details className="tea-shared-facts">
                <summary>本业务共享信息 · {record.facts.length} 项</summary>
                {record.facts.map((fact) => (
                  <div className="tea-fact" key={fact.key}>
                    <span>
                      {fact.label}
                      <small>{fact.confirmed ? "已确认" : "待确认"}</small>
                    </span>
                    <strong>{String(fact.value)}</strong>
                    <p>来源：{fact.source}</p>
                  </div>
                ))}
              </details>
            </div>
          )}
        </div>
        <div
          id="workspace-library"
          role="tabpanel"
          aria-label="业务资料库"
          hidden={tab !== "library"}
        >
          <Library
            ownerId={accountId}
            cases={[record]}
            initialCaseId={record.id}
            scopeLocked
            businessOnly
            active={tab === "library" && panelVisible}
            uploadedDocument={uploadedDocument}
            onDocumentsChange={updateLibraryDocuments}
            revision={record.revision + record.documents.length}
            onUpload={onUpload}
          />
        </div>
        <div
          id="workspace-document"
          role="tabpanel"
          aria-label="业务文稿"
          hidden={tab !== "document"}
        >
          {artifact ? (
            <>
              <Button
                variant="ghost"
                className="tea-panel-back"
                onClick={() => setArtifactId("")}
              >
                <ArrowLeft size={14} />
                返回文稿列表
              </Button>
              <ArtifactPanel
                key={artifact.id}
                artifact={artifact}
                onSaved={updateArtifact}
              />
            </>
          ) : (
            <div className="tea-business-artifacts">
              <h3>业务文稿</h3>
              <p className="tea-small-note">
                在会话中告诉助手要新建什么文稿，生成后会保存在这里。
              </p>
              {record.artifacts.length ? (
                record.artifacts.map((item) => (
                  <button
                    className="tea-artifact-list-item"
                    key={item.id}
                    onClick={() => setArtifactId(item.id)}
                  >
                    <FileText size={18} />
                    <span>
                      <strong>{item.title}</strong>
                      <small>
                        {item.confirmed ? "已确认" : "草稿"} · v{item.version} ·{" "}
                        {dateLabel(item.updated_at)}
                      </small>
                    </span>
                    <ChevronRight size={14} />
                  </button>
                ))
              ) : (
                <Empty icon={<FileText size={28} />} title="还没有文稿">
                  例如在会话中说“帮我新建一份出口资料清单”。
                </Empty>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
  return (
    <div
      className={`tea-workspace tea-node-workspace ${panelVisible ? "" : "tea-panel-hidden"}`}
    >
      <SidebarTrigger
        className="tea-workspace-nav-trigger md:hidden"
        aria-label="展开导航"
      />
      {((overlay && !mobileOpen) || (!overlay && !panelOpen)) && (
        <Button
          className="tea-workspace-panel-trigger"
          variant="ghost"
          size="icon"
          style={{ position: "absolute", top: 8, right: 20, left: "auto" }}
          aria-label="展开业务工作区"
          aria-expanded={overlay ? mobileOpen : panelOpen}
          onClick={() => {
            if (overlay) setMobileOpen(true);
            else setPanelOpen(true);
          }}
        >
          <PanelRightOpen size={17} />
        </Button>
      )}
      <ResizablePanelGroup
        orientation="horizontal"
        className="tea-workspace-panels"
        onLayoutChanged={(layout) => {
          const previous = previousDetailSize.current;
          previousDetailSize.current = layout["tea-details"] ?? 0;
          if (
            panelOpenRef.current &&
            previous > 0 &&
            layout["tea-details"] === 0
          )
            setPanelOpen(false);
        }}
      >
        <ResizablePanel
          id="conversation"
          minSize={overlay ? "100%" : "340px"}
          defaultSize="70%"
        >
          {guideError?.stageId === stage.id && isGuide ? (
            <ErrorNotice
              message={guideError.message}
              onRetry={() => setGuideAttempt((value) => value + 1)}
            />
          ) : stage.status === "locked" ? (
            <Empty icon={<LockKeyhole size={28} />} title="这个节点尚未解锁">
              先完成当前节点的必办任务，进度通过核验后会自动共享。
            </Empty>
          ) : currentConversation ? (
            <StageChat
              key={`${stage.id}:${currentConversation}`}
              accountId={accountId}
              record={record}
              stage={stage}
              conversationId={currentConversation}
              guide={isGuide}
              taskId={taskId}
              onTask={selectTask}
              onUpdate={onUpdate}
              onUpload={onUpload}
              capabilities={capabilities}
            />
          ) : (
            <Busy label="正在打开引导会话…" />
          )}
        </ResizablePanel>
        <ResizableHandle
          withHandle
          disabled={!panelOpen || overlay}
          className={`tea-resize-handle ${panelOpen && !overlay ? "" : "pointer-events-none opacity-0"}`}
        />
        <ResizablePanel
          id="tea-details"
          panelRef={panelRef}
          collapsible
          collapsedSize={0}
          minSize="280px"
          maxSize="55%"
          defaultSize="360px"
          onResize={(size) => {
            if (panelOpenRef.current && size.asPercentage > 0)
              openPanelSize.current = `${size.asPercentage}%`;
          }}
        >
          <div className="tea-details-content" hidden={!panelOpen || overlay}>
            {!overlay && panel}
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
      {overlay && (
        <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
          <SheetContent className="tea-theme tea-mobile-panel">
            <SheetHeader className="sr-only">
              <SheetTitle>业务工作区</SheetTitle>
              <SheetDescription>
                查看节点任务、业务资料与文稿。
              </SheetDescription>
            </SheetHeader>
            {panel}
          </SheetContent>
        </Sheet>
      )}
    </div>
  );
}
