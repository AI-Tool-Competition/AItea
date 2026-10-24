"use client";

import {
  Check,
  ChevronDown,
  CircleAlert,
  History,
  LoaderCircle,
  RotateCcw,
  Square,
} from "lucide-react";
import { useState } from "react";

import {
  Task,
  TaskContent,
  TaskItem,
  TaskTrigger,
} from "@/components/ai-elements";
import type { ToolCall } from "@/core/tea/types";

const labels = {
  running: "进行中",
  retrying: "等待重试",
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

export function ToolActivity({
  tools,
  running,
  cached = false,
}: {
  tools: readonly ToolCall[];
  running: boolean;
  cached?: boolean;
}) {
  const [expanded, setExpanded] = useState<boolean | null>(null);
  if (!tools.length) return null;
  const active =
    running &&
    tools.some(
      (tool) => tool.status === "running" || tool.status === "retrying",
    );
  const completed = tools.filter((tool) => tool.status === "completed").length;
  return (
    <Task
      open={
        expanded ?? (running || tools.some((tool) => tool.status === "failed"))
      }
      onOpenChange={setExpanded}
      className="border-border/60 bg-muted/20 mt-2 min-w-0 rounded-xl border px-3 py-2"
    >
      <TaskTrigger title="工具调用">
        <button
          type="button"
          className="text-muted-foreground flex w-full items-center gap-2 text-left text-xs"
        >
          {cached ? (
            <History className="size-3.5" aria-hidden="true" />
          ) : active ? (
            <LoaderCircle
              className="size-3.5 animate-spin motion-reduce:animate-none"
              aria-hidden="true"
            />
          ) : tools.some((tool) => tool.status === "failed") ? (
            <CircleAlert
              className="text-destructive size-3.5"
              aria-hidden="true"
            />
          ) : tools.some((tool) => tool.status !== "completed") ? (
            <Square className="size-3.5" aria-hidden="true" />
          ) : (
            <Check className="size-3.5" aria-hidden="true" />
          )}
          <span className="flex-1">{cached ? "沿用工具记录" : "工具调用"}</span>
          <span className="tabular-nums">
            {completed} / {tools.length}
          </span>
          <ChevronDown
            className="size-3.5 transition-transform group-data-[state=open]:rotate-180 motion-reduce:transition-none"
            aria-hidden="true"
          />
        </button>
      </TaskTrigger>
      <TaskContent className="motion-reduce:animate-none">
        <ol
          aria-label="工具调用顺序"
          aria-live={running ? "polite" : "off"}
          aria-relevant="additions text"
          className="space-y-3"
        >
          {tools.map((tool, index) => {
            const unfinished =
              tool.status === "running" || tool.status === "retrying";
            const status = unfinished && !running ? "cancelled" : tool.status;
            const Icon = icons[status];
            return (
              <li key={tool.id} data-tool-id={tool.id} data-state={status}>
                <TaskItem className="text-xs">
                  <div className="grid grid-cols-[auto_auto_minmax(0,1fr)] items-center gap-x-2 gap-y-1 sm:grid-cols-[auto_auto_minmax(0,1fr)_auto]">
                    <span
                      aria-hidden="true"
                      className="text-[10px] tabular-nums opacity-60"
                    >
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <Icon
                      aria-hidden="true"
                      className={`size-3.5 shrink-0 ${active && unfinished ? "animate-spin motion-reduce:animate-none" : ""} ${status === "failed" ? "text-destructive" : status === "completed" ? "text-emerald-600 dark:text-emerald-400" : ""}`}
                    />
                    <span className="text-foreground min-w-0 flex-1 font-medium break-words">
                      {tool.label}
                    </span>
                    <span className="col-start-3 flex flex-wrap items-center gap-x-2 text-[11px] sm:col-start-4">
                      <span>{labels[status]}</span>
                      {!unfinished && (
                        <span className="tabular-nums opacity-70">
                          {tool.duration_ms < 1000
                            ? "少于 1 秒"
                            : `${(tool.duration_ms / 1000).toFixed(1)} 秒`}
                        </span>
                      )}
                    </span>
                  </div>
                  <p className="mt-1 pl-10 leading-relaxed break-words">
                    {unfinished && !running
                      ? "调用已中断，结果尚未确认。"
                      : tool.detail}
                  </p>
                  {tool.attempt > 1 && (
                    <p className="mt-1 pl-10 text-[11px] opacity-70">
                      第 {tool.attempt} 次尝试 · 最多 {tool.max_attempts} 次
                    </p>
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
