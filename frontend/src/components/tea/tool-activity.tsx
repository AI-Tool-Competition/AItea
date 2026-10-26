"use client";

import {
  ArrowRight,
  Check,
  ChevronDown,
  CircleAlert,
  FileSearch,
  FileText,
  Globe,
  History,
  Image,
  ListChecks,
  LoaderCircle,
  PenLine,
  RotateCcw,
  Search,
  Sparkles,
  Square,
  type LucideIcon,
} from "lucide-react";
import { memo, useId, useState } from "react";

import {
  Task,
  TaskContent,
  TaskItem,
  TaskTrigger,
} from "@/components/ai-elements";
import { safeSourceUrl } from "@/core/tea/api";
import type { ToolCall, ToolOutputPreview, TraceStep } from "@/core/tea/types";

// Only actual work and provider-supplied public summaries are visible.
// Routing, memory, private payloads and opaque thought signatures stay internal.
const activities: Partial<
  Record<ToolCall["name"], { label: string; active: string; icon: LucideIcon }>
> = {
  agent_turn: { label: "思考摘要", active: "正在思考", icon: Sparkles },
  search_documents: { label: "查找资料", active: "正在查找资料", icon: Search },
  search_legal_evidence: {
    label: "查找依据",
    active: "正在查找依据",
    icon: FileSearch,
  },
  check_materials: {
    label: "核对材料",
    active: "正在核对材料",
    icon: ListChecks,
  },
  generate_guidance: {
    label: "整理办理建议",
    active: "正在整理办理建议",
    icon: ListChecks,
  },
  guide_business: {
    label: "梳理办理要求",
    active: "正在梳理办理要求",
    icon: ListChecks,
  },
  analyze_legal: {
    label: "核对适用依据",
    active: "正在核对适用依据",
    icon: FileSearch,
  },
  draft_document: { label: "准备文稿", active: "正在准备文稿", icon: PenLine },
  get_handoff: {
    label: "核对下一步安排",
    active: "正在核对下一步安排",
    icon: ArrowRight,
  },
  list_documents: {
    label: "查看已有资料",
    active: "正在查看已有资料",
    icon: FileText,
  },
  read_document: { label: "阅读资料", active: "正在阅读资料", icon: FileText },
  inspect_image: { label: "查看图片", active: "正在查看图片", icon: Image },
  web_search: {
    label: "搜索公开信息",
    active: "正在搜索公开信息",
    icon: Globe,
  },
};
const labels = {
  running: "进行中",
  retrying: "正在重试",
  completed: "已完成",
  failed: "未完成",
  cancelled: "已停止",
};
const icons = {
  running: LoaderCircle,
  retrying: RotateCcw,
  completed: Check,
  failed: CircleAlert,
  cancelled: Square,
};
type ActivityCall = ToolCall & { cached?: boolean };

/** Flatten real calls without exposing the agent loop or six-stage analysis. */
export const MessageActivity = memo(function MessageActivity({
  steps,
  running = false,
  answering = false,
  incomplete = false,
  stopped = false,
}: {
  steps: readonly TraceStep[];
  running?: boolean;
  answering?: boolean;
  incomplete?: boolean;
  stopped?: boolean;
}) {
  const calls = new Map<string, ActivityCall>();
  for (const step of steps)
    for (const tool of step.tools ?? [])
      calls.set(tool.id, { ...tool, cached: step.status === "cached" });
  return (
    <ToolActivity
      tools={[...calls.values()]}
      running={running}
      answering={answering}
      preparing={running}
      incomplete={incomplete || steps.some((step) => step.status === "failed")}
      stopped={stopped || steps.some((step) => step.status === "cancelled")}
    />
  );
});

// Follow new work until the answer starts. The first answer closes progress;
// later events respect the reader's choice until the turn finishes.
function useFollowingDisclosure(
  running: boolean,
  active: string[],
  answering = false,
) {
  const activity = active.join("\n");
  const [state, setState] = useState({
    running,
    activity,
    answering,
    open: running && !answering,
  });
  let next = state;
  if (state.running !== running)
    next = { running, activity, answering, open: running && !answering };
  else if (!state.answering && answering)
    next = { running, activity, answering, open: false };
  else if (state.activity !== activity || state.answering !== answering) {
    const previous = new Set(state.activity.split("\n"));
    const started =
      running && !answering && active.some((key) => !previous.has(key));
    next = { running, activity, answering, open: started ? true : state.open };
  }
  if (next !== state) setState(next);
  return {
    open: next.open,
    onOpenChange: (open: boolean) =>
      setState({ running, activity, answering, open }),
  };
}

export function ToolActivity({
  tools,
  running,
  answering = false,
  preparing = false,
  cached = false,
  incomplete = false,
  stopped = false,
}: {
  tools: readonly ActivityCall[];
  running: boolean;
  answering?: boolean;
  preparing?: boolean;
  cached?: boolean;
  incomplete?: boolean;
  stopped?: boolean;
}) {
  const id = useId();
  // Preserve the initial preparation panel through the final collapse even
  // when a text-only reply has no trace. Historical empty traces stay hidden.
  const [startedHere] = useState(preparing);
  const visible = tools.flatMap((tool) => {
    if (tool.name === "agent_turn" && !tool.output_preview?.text) return [];
    const view = Object.hasOwn(activities, tool.name)
      ? activities[tool.name]
      : undefined;
    if (!view) return [];
    const unfinished = tool.status === "running" || tool.status === "retrying";
    const status = unfinished && !running ? "cancelled" : tool.status;
    return [{ ...tool, view, status, cached: cached || tool.cached }];
  });
  const active = visible.filter(
    (tool) => tool.status === "running" || tool.status === "retrying",
  );
  const model = tools.filter((tool) => tool.name === "agent_turn").at(-1);
  const thinking = model?.status === "running" || model?.status === "retrying";
  const disclosure = useFollowingDisclosure(
    running,
    active.map((tool) => `${tool.id}:${tool.attempt}`),
    answering,
  );
  if (!visible.length && !preparing && !startedHere && !model) return null;
  const failed = incomplete || visible.some((tool) => tool.status === "failed");
  const cancelled =
    stopped || visible.some((tool) => tool.status === "cancelled");
  const allCached = visible.length > 0 && visible.every((tool) => tool.cached);
  const completedCount = visible.filter(
    (tool) => tool.name !== "agent_turn",
  ).length;
  const heading = running
    ? answering
      ? "正在回复"
      : active.length > 1
        ? `正在处理 ${active.length} 项工作`
        : (active[0]?.view.active ??
          (thinking
            ? "正在思考"
            : visible.length
              ? "正在整理回复"
              : "正在准备回复"))
    : failed
      ? "部分处理未完成"
      : cancelled
        ? "处理已停止"
        : allCached
          ? "已沿用处理记录"
          : completedCount
            ? `已完成 ${completedCount} 项处理`
            : "已完成回复";
  const HeaderIcon = running
    ? LoaderCircle
    : failed
      ? CircleAlert
      : cancelled
        ? Square
        : allCached
          ? History
          : Check;
  return (
    <Task {...disclosure} className="tea-tool-activity">
      <TaskTrigger title={heading}>
        <button
          id={id + "-trigger"}
          type="button"
          aria-label={`${heading} · 处理进度`}
          aria-controls={id + "-content"}
          className="tea-tool-trigger"
        >
          <HeaderIcon
            className={running ? "tea-spin size-4" : "size-4"}
            aria-hidden="true"
          />
          <span className="tea-tool-heading" role="status" aria-atomic="true">
            {heading}
          </span>
          <ChevronDown
            className="tea-tool-chevron size-3.5"
            aria-hidden="true"
          />
        </button>
      </TaskTrigger>
      <TaskContent
        id={id + "-content"}
        role="region"
        aria-labelledby={id + "-trigger"}
        className="tea-tool-content motion-reduce:animate-none"
      >
        {visible.length > 0 && (
          <ol
            aria-label="处理进度"
            aria-live={running ? "polite" : "off"}
            aria-relevant="additions text"
            className="tea-tool-list"
          >
            {visible.map((tool) => (
              <ToolRow
                key={tool.id}
                tool={tool}
                running={running}
                answering={answering}
              />
            ))}
          </ol>
        )}
        {!active.length && !answering && running && (
          <p className="tea-tool-preparation">等待模型响应…</p>
        )}
      </TaskContent>
    </Task>
  );
}

function ToolRow({
  tool,
  running,
  answering,
}: {
  tool: ActivityCall & {
    view: NonNullable<(typeof activities)[ToolCall["name"]]>;
  };
  running: boolean;
  answering: boolean;
}) {
  const id = useId();
  const working = tool.status === "running" || tool.status === "retrying";
  const disclosure = useFollowingDisclosure(
    running && working,
    working ? [`${tool.id}:${tool.attempt}`] : [],
    answering,
  );
  const Icon = tool.view.icon;
  const StatusIcon = tool.cached ? History : icons[tool.status];
  const preview = tool.output_preview;
  return (
    <li data-tool-id={tool.id} data-state={tool.status}>
      <Task {...disclosure} className="tea-tool-row">
        <TaskTrigger title={tool.view.label}>
          <button
            id={id + "-trigger"}
            type="button"
            className="tea-tool-item"
            aria-label={`${tool.view.label} · ${tool.cached ? "沿用结果" : labels[tool.status]}`}
            aria-controls={id + "-content"}
          >
            <Icon className="tea-tool-kind size-4" aria-hidden="true" />
            <span className="tea-tool-description">
              <span className="tea-tool-label">{tool.view.label}</span>
            </span>
            <span className="tea-tool-state">
              <StatusIcon
                className={working ? "tea-spin size-3.5" : "size-3.5"}
                aria-hidden="true"
              />
              <span>{tool.cached ? "沿用结果" : labels[tool.status]}</span>
              {tool.status === "completed" &&
                !tool.cached &&
                tool.duration_ms > 0 && (
                  <span className="tea-tool-duration">
                    {tool.duration_ms < 1000
                      ? "少于 1 秒"
                      : `${(tool.duration_ms / 1000).toFixed(1)} 秒`}
                  </span>
                )}
            </span>
            <ChevronDown
              className="tea-tool-chevron size-3.5"
              aria-hidden="true"
            />
          </button>
        </TaskTrigger>
        <TaskContent
          id={id + "-content"}
          role="region"
          aria-labelledby={id + "-trigger"}
          className="tea-tool-detail"
        >
          <TaskItem>
            {preview ? (
              <ToolOutput preview={preview} />
            ) : working ? (
              <p>
                {tool.status === "retrying"
                  ? "正在重新尝试，收到结果后继续。"
                  : `${tool.view.active}，收到结果后继续。`}
              </p>
            ) : tool.status === "failed" ? (
              <p>本次处理未完成，请结合回复中的说明继续。</p>
            ) : tool.status === "cancelled" ? (
              <p>处理已停止，结果尚未确认。</p>
            ) : (
              <p>本次处理已完成，未返回可展示的结果片段。</p>
            )}
          </TaskItem>
        </TaskContent>
      </Task>
    </li>
  );
}

const resultStates: Record<string, string> = {
  passed: "已满足",
  missing: "待补充",
  conflict: "有冲突",
  pending: "待办理",
  active: "办理中",
  completed: "已完成",
  locked: "待解锁",
  undetermined: "尚无法判断",
  conditional: "有条件适用",
  satisfied: "已满足",
  unsatisfied: "尚未满足",
  not_applicable: "不适用",
};

function ToolOutput({ preview }: { preview: ToolOutputPreview }) {
  return (
    <div className="tea-tool-output" aria-label="执行结果">
      {preview.text && <p>{preview.text}</p>}
      {!!preview.items.length && (
        <div className="tea-tool-output-items">
          {preview.items.map((item, index) => {
            const url = safeSourceUrl(item.url);
            const status =
              item.status && Object.hasOwn(resultStates, item.status)
                ? resultStates[item.status]
                : undefined;
            return (
              <div key={index} className="tea-tool-output-item">
                {item.label &&
                  (url ? (
                    <a href={url} target="_blank" rel="noopener noreferrer">
                      {item.label}
                    </a>
                  ) : (
                    <strong>{item.label}</strong>
                  ))}
                {status && (
                  <span className="tea-tool-result-status">{status}</span>
                )}
                {item.text && <p>{item.text}</p>}
              </div>
            );
          })}
        </div>
      )}
      {preview.notice && (
        <p className="tea-tool-output-notice">{preview.notice}</p>
      )}
      {preview.truncated && (
        <p className="tea-tool-output-notice">此处为本次执行结果的节选。</p>
      )}
    </div>
  );
}
