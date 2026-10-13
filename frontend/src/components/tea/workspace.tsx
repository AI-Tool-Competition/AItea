"use client";
import {
  Check,
  CheckCheck,
  ChevronRight,
  FolderOpen,
  LockKeyhole,
  PanelRightClose,
  PanelRightOpen,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
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
import { teaRequest } from "@/core/tea/api";
import {
  type Artifact,
  type Capabilities,
  type CaseDetail,
} from "@/core/tea/types";
import { useIsMobile } from "@/hooks/use-mobile";

import { ArtifactPanel } from "./artifact-panel";
import { dateLabel, ErrorNotice } from "./shared";
import { StageChat } from "./stage-chat";
export { clearChatDrafts } from "@/core/tea/use-stage-chat";
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
                      : stage.status === "follow_up"
                        ? "待收尾"
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
                  : stage.status === "follow_up"
                    ? "后续阶段可继续；本阶段仍有必办收尾任务，完成后才能结案。"
                    : stage.description}
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

export function Workspace({
  accountId,
  record,
  stageId,
  setStageId,
  capabilities,
  onUpdate,
  onUpload,
}: {
  record: CaseDetail;
  accountId: string;
  stageId: string;
  setStageId: (id: string) => void;
  capabilities: Capabilities;
  onUpdate: (record: CaseDetail) => void;
  onUpload: () => void;
}) {
  const [panelOpen, setPanelOpen] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [tab, setTab] = useState<PanelTab>("flow");
  const [taskId, setTaskId] = useState("");
  const [artifactId, setArtifactId] = useState("");
  const isMobile = useIsMobile();
  const panelRef = usePanelRef();
  const panelOpenRef = useRef(false);
  const openPanelSize = useRef("40%");
  const stage = record.stages.find((s) => s.id === stageId) ?? record.stages[0];
  const task = stage?.tasks.find((t) => t.id === taskId);
  const selectedArtifact =
    record.artifacts.find((a) => a.id === artifactId) ?? record.artifacts[0];
  useEffect(() => {
    panelOpenRef.current = !isMobile && panelOpen;
    if (!isMobile && panelOpen) panelRef.current?.resize(openPanelSize.current);
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
              onClick={() => {
                setTab(id);
              }}
            >
              {label}
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
          title="收起流程"
        >
          <PanelRightClose size={17} />
        </Button>
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
      <SidebarTrigger
        className="tea-workspace-navigation md:hidden"
        aria-label="展开导航"
      />
      {(!panelOpen || isMobile) && (
        <div className="tea-workspace-panel-actions">
          <Button
            variant="ghost"
            size="icon"
            className="tea-workspace-panel-toggle"
            aria-label="展开流程"
            title="展开流程"
            aria-expanded={isMobile ? mobileOpen : panelOpen}
            onClick={() => {
              if (isMobile) setMobileOpen(true);
              else setPanelOpen(true);
            }}
          >
            <PanelRightOpen size={17} />
          </Button>
        </div>
      )}
      <ResizablePanelGroup
        orientation="horizontal"
        className="tea-workspace-panels"
        onLayoutChanged={(layout) => {
          // A completed drag can collapse the panel; resize callbacks only
          // remember its width and must not reverse a requested close.
          if (panelOpenRef.current && layout["tea-details"] === 0)
            setPanelOpen(false);
        }}
      >
        <ResizablePanel id="conversation" minSize="30%" defaultSize="100%">
          <StageChat
            accountId={accountId}
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
          disabled={!panelOpen || isMobile}
          className={`tea-resize-handle ${panelOpen && !isMobile ? "" : "pointer-events-none opacity-0"}`}
        />
        <ResizablePanel
          id="tea-details"
          panelRef={panelRef}
          collapsible
          collapsedSize={0}
          minSize="20%"
          maxSize="70%"
          defaultSize="0%"
          onResize={(size) => {
            if (panelOpenRef.current && size.asPercentage > 0) {
              openPanelSize.current = `${size.asPercentage}%`;
            }
          }}
        >
          <div className="tea-details-content" hidden={!panelOpen || isMobile}>
            {!isMobile && panel}
          </div>
        </ResizablePanel>
      </ResizablePanelGroup>
      {isMobile && (
        <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
          <SheetContent className="tea-theme tea-mobile-panel">
            <SheetHeader className="sr-only">
              <SheetTitle>办理流程与资料</SheetTitle>
              <SheetDescription>
                查看任务、核验进度、编辑文稿和业务档案。
              </SheetDescription>
            </SheetHeader>
            {panel}
          </SheetContent>
        </Sheet>
      )}
    </div>
  );
}
