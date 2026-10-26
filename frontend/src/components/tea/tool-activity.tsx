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
  Square,
  type LucideIcon,
} from "lucide-react";
import { useId, useState } from "react";

import {
  Task,
  TaskContent,
  TaskItem,
  TaskTrigger,
} from "@/components/ai-elements";
import { safeSourceUrl } from "@/core/tea/api";
import type { ToolCall, ToolOutputPreview, TraceStep } from "@/core/tea/types";

// Only actual user-relevant work is visible. Routing, memory, plans and model
// turns stay internal, including in saved messages. Never render raw payloads.
const activities: Partial<
  Record<ToolCall["name"], { label: string; active: string; icon: LucideIcon }>
> = {
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
export function MessageActivity({
  steps,
  running = false,
  incomplete = false,
}: {
  steps: readonly TraceStep[];
  running?: boolean;
  incomplete?: boolean;
}) {
  const calls = new Map<string, ActivityCall>();
  for (const step of steps)
    for (const tool of step.tools ?? [])
      calls.set(tool.id, { ...tool, cached: step.status === "cached" });
  return (
    <ToolActivity
      tools={[...calls.values()]}
      running={running}
      incomplete={incomplete || steps.some((step) => step.status === "failed")}
      stopped={steps.some((step) => step.status === "cancelled")}
    />
  );
}

export function ToolActivity({
  tools,
  running,
  cached = false,
  incomplete = false,
  stopped = false,
}: {
  tools: readonly ActivityCall[];
  running: boolean;
  cached?: boolean;
  incomplete?: boolean;
  stopped?: boolean;
}) {
  const id = useId();
  const [expanded, setExpanded] = useState<boolean | null>(null);
  const visible = tools.flatMap((tool) => {
    const view = Object.hasOwn(activities, tool.name)
      ? activities[tool.name]
      : undefined;
    if (!view) return [];
    const unfinished = tool.status === "running" || tool.status === "retrying";
    const status = unfinished && !running ? "cancelled" : tool.status;
    return [{ ...tool, view, status, cached: cached || tool.cached }];
  });
  if (!visible.length) return null;
  const active = visible.filter(
    (tool) => tool.status === "running" || tool.status === "retrying",
  );
  const failed = incomplete || visible.some((tool) => tool.status === "failed");
  const cancelled =
    stopped || visible.some((tool) => tool.status === "cancelled");
  const allCached = visible.every((tool) => tool.cached);
  const heading = running
    ? active.length > 1
      ? `正在处理 ${active.length} 项工作`
      : (active[0]?.view.active ?? "正在整理回复")
    : failed
      ? "部分处理未完成"
      : cancelled
        ? "处理已停止"
        : allCached
          ? "已沿用处理记录"
          : `已完成 ${visible.length} 项处理`;
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
    <Task
      open={expanded ?? running}
      onOpenChange={setExpanded}
      className="tea-tool-activity"
    >
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
            className="size-3.5 transition-transform group-data-[state=open]:rotate-180 motion-reduce:transition-none"
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
        <ol
          aria-label="处理进度"
          aria-live={running ? "polite" : "off"}
          aria-relevant="additions text"
          className="tea-tool-list"
        >
          {visible.map((tool) => {
            const working =
              tool.status === "running" || tool.status === "retrying";
            const Icon = tool.view.icon;
            const StatusIcon = tool.cached ? History : icons[tool.status];
            return (
              <li key={tool.id} data-tool-id={tool.id} data-state={tool.status}>
                <TaskItem className="tea-tool-item">
                  <Icon className="tea-tool-kind size-4" aria-hidden="true" />
                  <div className="tea-tool-description">
                    <span className="tea-tool-label">{tool.view.label}</span>
                    {tool.status === "failed" && (
                      <p>本次处理未完成，请结合回复中的说明继续。</p>
                    )}
                    {tool.status === "cancelled" && (
                      <p>处理已停止，结果尚未确认。</p>
                    )}
                  </div>
                  <span className="tea-tool-state">
                    <StatusIcon
                      className={working ? "tea-spin size-3.5" : "size-3.5"}
                      aria-hidden="true"
                    />
                    <span>
                      {tool.cached ? "沿用结果" : labels[tool.status]}
                    </span>
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
                  {tool.status === "completed" && tool.output_preview && (
                    <ToolOutput preview={tool.output_preview} />
                  )}
                </TaskItem>
              </li>
            );
          })}
        </ol>
      </TaskContent>
    </Task>
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
  const [expanded, setExpanded] = useState(false);
  const long =
    Array.from(preview.text).length > 180 ||
    preview.items.length > 2 ||
    preview.items.some((item) => Array.from(item.text).length > 160);
  const excerpt = (text: string, limit: number) => {
    const characters = Array.from(text);
    return expanded || characters.length <= limit
      ? text
      : characters.slice(0, limit).join("") + "…";
  };
  return (
    <div className="tea-tool-output" aria-label="执行结果">
      {preview.text && <p>{excerpt(preview.text, 180)}</p>}
      {!!preview.items.length && (
        <div className="tea-tool-output-items">
          {(expanded ? preview.items : preview.items.slice(0, 2)).map(
            (item, index) => {
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
                  {item.text && <p>{excerpt(item.text, 160)}</p>}
                </div>
              );
            },
          )}
        </div>
      )}
      {preview.notice && (
        <p className="tea-tool-output-notice">{preview.notice}</p>
      )}
      {preview.truncated && (
        <p className="tea-tool-output-notice">此处为本次执行结果的节选。</p>
      )}
      {long && (
        <button
          type="button"
          className="tea-tool-output-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "收起结果" : "展开结果"}
          <ChevronDown
            size={12}
            aria-hidden="true"
            className={expanded ? "rotate-180" : undefined}
          />
        </button>
      )}
    </div>
  );
}
