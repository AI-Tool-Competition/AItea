"use client";

import { type LegalResult } from "@/core/tea/types";

import { CitationCard } from "./library";
import { Trace } from "./shared";

export const legalStatusLabel = {
  running: "分析中",
  completed: "分析已完成",
  needs_input: "待补充事实",
  needs_review: "待复核",
  failed: "分析未完成",
  cancelled: "已停止",
};
const conclusionLabels = {
  supported: "有依据支持",
  unsupported: "有依据不支持",
  conditional: "附条件结论",
  undetermined: "尚无法判断",
};
const actionLabels = {
  mandatory: "法定义务",
  conditional: "满足条件后办理",
  recommended: "办理建议",
  commercial: "商业选择",
  unverified: "待核实建议",
};
const conditionLabels = {
  met: "已满足",
  not_met: "未满足",
  unknown: "待核实",
  not_applicable: "不适用",
};

export function LegalResultView({
  result,
  compact = false,
}: {
  result: LegalResult;
  compact?: boolean;
}) {
  if (compact)
    return (
      <div className="tea-node-summary">
        {result.issues.slice(0, 3).map((issue) => (
          <div key={issue.id}>
            <p>
              <span className="ta-badge">{conclusionLabels[issue.status]}</span>{" "}
              {issue.conclusion}
            </p>
            {issue.actions.length > 0 && (
              <ul>
                {issue.actions.slice(0, 3).map((action) => (
                  <li key={action.id}>
                    <span>{actionLabels[action.nature]}</span> · {action.title}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
        {result.issues.length > 3 && (
          <p className="tea-legal-note">
            另有 {result.issues.length - 3} 项问题，可展开详细结论查看。
          </p>
        )}
        {result.followups.length > 0 && (
          <p className="tea-legal-note">
            还需核对：
            {result.followups
              .slice(0, 3)
              .map((item) => item.label)
              .join("、")}
            {result.followups.length > 3 ? "等" : ""}。下方可直接补充。
          </p>
        )}
        {!result.issues.length && result.output && (
          <p className="tea-legal-prose">{result.output}</p>
        )}
      </div>
    );
  return (
    <div className="tea-legal-result">
      <p className="tea-legal-note">
        {result.assistant_mode === "live"
          ? "模型辅助分析 · 结论状态逐项列出"
          : "演示模式 · 未调用大模型"}
      </p>
      {result.issues.map((issue) => (
        <article key={issue.id} className="tea-legal-issue">
          <header>
            <span className="ta-badge">
              {issue.kind === "legal" ? "法律问题" : "信息问题"}
            </span>
            <span className="ta-badge">{conclusionLabels[issue.status]}</span>
            {issue.needs_review && <span className="ta-badge">待复核</span>}
          </header>
          <h4>{issue.question}</h4>
          <p className="tea-legal-note">
            {[
              ...issue.domains.map((domain) =>
                domain === "general" ? "一般问题" : domain,
              ),
              ...issue.jurisdictions,
            ].join(" · ") || "适用范围待确认"}
          </p>
          <p className="tea-legal-conclusion">{issue.conclusion}</p>
          {issue.explanation && <p>{issue.explanation}</p>}
          {issue.conditions.length > 0 && (
            <details>
              <summary>适用条件 · {issue.conditions.length} 项</summary>
              <ul>
                {issue.conditions.map((condition, i) => (
                  <li key={i}>
                    <strong>
                      {condition.label} · {conditionLabels[condition.state]}
                    </strong>
                    <p>{condition.reason}</p>
                    {condition.citation_ids.length > 0 && (
                      <small>引用：{condition.citation_ids.join("、")}</small>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {issue.deadlines.map((deadline) => (
            <div className="tea-legal-deadline" key={deadline.id}>
              <strong>
                {deadline.label} ·{" "}
                {deadline.status === "calculated"
                  ? "已计算"
                  : deadline.status === "conditional"
                    ? "有待确认的前提"
                    : "暂无法计算"}
              </strong>
              {deadline.date && (
                <p>
                  {deadline.date} · {deadline.timezone}
                </p>
              )}
              {deadline.remaining_days !== undefined &&
                deadline.remaining_days !== null && (
                  <p>
                    {deadline.status === "calculated" ? "" : "按当前假设："}
                    {deadline.remaining_days < 0
                      ? `已过 ${Math.abs(deadline.remaining_days)} 天`
                      : `剩余 ${deadline.remaining_days} 天`}
                    {deadline.risk === "imminent"
                      ? " · 期限临近"
                      : deadline.risk === "overdue"
                        ? " · 已逾期"
                        : ""}
                    {deadline.as_of ? `（截至 ${deadline.as_of}）` : ""}
                  </p>
                )}
              {[...deadline.assumptions, ...deadline.missing_prerequisites].map(
                (text, i) => (
                  <p key={i}>{text}</p>
                ),
              )}
              {deadline.citation_ids.length > 0 && (
                <small>依据：{deadline.citation_ids.join("、")}</small>
              )}
            </div>
          ))}
          {issue.actions.length > 0 && (
            <div className="tea-legal-actions">
              <h5>后续办理</h5>
              {issue.actions.map((action) => (
                <div key={action.id}>
                  <span className="ta-badge">
                    {actionLabels[action.nature]}
                  </span>
                  <strong>{action.title}</strong>
                  <p>{action.details}</p>
                </div>
              ))}
            </div>
          )}
          {issue.gaps.length > 0 && (
            <ul className="tea-legal-gaps">
              {issue.gaps.map((gap, i) => (
                <li key={i}>{gap}</li>
              ))}
            </ul>
          )}
          {issue.citations.length > 0 && (
            <details>
              <summary>原文依据 · {issue.citations.length} 项</summary>
              {issue.citations.map((citation, i) => (
                <div key={citation.id ?? `${citation.document_id}:${i}`}>
                  {citation.id && <small>引用 {citation.id}</small>}
                  <CitationCard citation={citation} />
                </div>
              ))}
            </details>
          )}
        </article>
      ))}
      {!result.issues.length && result.output && (
        <p className="tea-legal-prose">{result.output}</p>
      )}
      {result.gaps.length > 0 && (
        <div className="tea-legal-gaps">
          <strong>当前分析的缺口</strong>
          <ul>
            {result.gaps.map((gap, i) => (
              <li key={i}>{gap}</li>
            ))}
          </ul>
        </div>
      )}
      <Trace steps={result.trace} />
      {result.trace.some((step) => step.input_data ?? step.output_data) && (
        <details className="tea-legal-public-trace">
          <summary>查看各阶段输入与结果</summary>
          {result.trace.map((step) => (
            <details key={step.id}>
              <summary>{step.label}</summary>
              {step.input_data && (
                <>
                  <h5>输入</h5>
                  <pre>{JSON.stringify(step.input_data, null, 2)}</pre>
                </>
              )}
              {step.output_data && (
                <>
                  <h5>结果</h5>
                  <pre>{JSON.stringify(step.output_data, null, 2)}</pre>
                </>
              )}
            </details>
          ))}
        </details>
      )}
    </div>
  );
}
