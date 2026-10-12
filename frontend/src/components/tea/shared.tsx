"use client";
import { AlertCircle, Check, ChevronDown, Leaf, Loader2 } from "lucide-react";
import { type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { type Check as CheckResult, type TraceStep } from "@/core/tea/types";

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className="tea-brand">
      <span className="tea-brand-mark">
        <Leaf size={22} strokeWidth={1.6} />
      </span>
      {!compact && <span>茶渡</span>}
    </div>
  );
}
export function Busy({ label = "正在读取…" }: { label?: string }) {
  return (
    <div className="tea-busy" role="status">
      <Loader2 size={19} className="tea-spin" />
      {label}
    </div>
  );
}
export function ErrorNotice({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="tea-error" role="alert">
      <AlertCircle size={17} />
      <span>{message}</span>
      {onRetry && <button onClick={onRetry}>重试</button>}
    </div>
  );
}
export function Empty({
  icon,
  title,
  children,
}: {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="tea-empty">
      {icon}
      <h3>{title}</h3>
      <div>{children}</div>
    </div>
  );
}
export function SectionHeading({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="tea-section-heading">
      <div>
        <h2>{title}</h2>
      </div>
      {children}
    </div>
  );
}
export function Checks({ checks }: { checks: CheckResult[] }) {
  return (
    <div className="tea-checks">
      {checks.map((check) => (
        <div
          className={`tea-check ${check.status}`}
          key={`${check.task_id ?? "task"}:${check.id}`}
        >
          <span>
            {check.status === "passed" ? (
              <Check size={15} />
            ) : (
              <AlertCircle size={15} />
            )}
          </span>
          <div>
            {check.task_title && (
              <small className="tea-check-task">{check.task_title}</small>
            )}
            <strong>{check.label}</strong>
            <p>{check.reason}</p>
            {check.evidence_refs.length > 0 && (
              <small>已关联 {check.evidence_refs.length} 项依据</small>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
export function Trace({ steps }: { steps: TraceStep[] }) {
  if (!steps.length) return null;
  return (
    <details className="tea-trace">
      <summary>
        <span>
          <Check size={13} />
          依据与检查记录 · {steps.length} 步
        </span>
        <ChevronDown size={14} />
      </summary>
      <div>
        {steps.map((step) => (
          <div key={step.id}>
            <span className={`tea-trace-dot ${step.status}`} />
            <div>
              <strong>{step.label}</strong>
              <p>{step.detail}</p>
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}
export function ActionButton({
  busy,
  children,
  ...props
}: React.ComponentProps<typeof Button> & { busy?: boolean }) {
  return (
    <Button {...props} disabled={props.disabled === true || busy === true}>
      {busy && <Loader2 className="tea-spin" size={15} />}
      {children}
    </Button>
  );
}
export const taskStatus = {
  pending: "未开始",
  in_progress: "办理中",
  needs_input: "待补充",
  completed: "已完成",
};
export function dateLabel(value: string, includeYear = false) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: includeYear ? "numeric" : undefined,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Shanghai",
  }).format(new Date(value));
}
