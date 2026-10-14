"use client";

import { ChevronDown } from "lucide-react";

import { conclusionLabels } from "@/core/tea/legal-labels";
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
  showTrace = true,
  showMode = true,
  detailsOnly = false,
}: {
  result: LegalResult;
  compact?: boolean;
  showTrace?: boolean;
  showMode?: boolean;
  detailsOnly?: boolean;
}) {
  if (compact)
    return (
      <div className="tea-node-summary">
        {result.issues.slice(0, 3).map((issue, index) => (
          <article className="tea-node-conclusion" key={issue.id}>
            <header>
              <span className="tea-node-issue-number">{index + 1}</span>
              <h5>
                {issue.domains.length
                  ? issue.domains
                      .map((domain) =>
                        domain === "general" ? "一般问题" : domain,
                      )
                      .join(" · ")
                  : issue.kind === "legal"
                    ? "法律分析"
                    : "业务信息"}
              </h5>
              <span className="tea-conclusion-state" data-status={issue.status}>
                {conclusionLabels[issue.status]}
                {issue.needs_review ? " · 待复核" : ""}
              </span>
              <details className="tea-node-question">
                <summary aria-label={`查看问题 ${index + 1} 原文`}>
                  问题原文 <ChevronDown size={12} aria-hidden="true" />
                </summary>
                <p>{issue.question}</p>
              </details>
            </header>
            <p>{issue.conclusion}</p>
            {issue.actions.length > 0 && (
              <ul>
                {issue.actions.slice(0, 3).map((action) => (
                  <li key={action.id}>
                    <span>{actionLabels[action.nature]}</span>
                    <span>{action.title}</span>
                  </li>
                ))}
              </ul>
            )}
          </article>
        ))}
        {result.issues.length > 3 && (
          <p className="tea-legal-note">
            另有 {result.issues.length - 3}{" "}
            项问题，可在「原文依据」中查看完整分析。
          </p>
        )}
        {!result.issues.length && result.output && (
          <p className="tea-legal-prose">{result.output}</p>
        )}
      </div>
    );
  const sourceKey = (source: LegalResult["citations"][number]) =>
    source.id ??
    JSON.stringify([
      source.document_id,
      source.document_version,
      source.locator,
    ]);
  const shownSources = new Set(
    result.issues.flatMap((issue) => issue.citations.map(sourceKey)),
  );
  const otherSources = result.citations.filter((source) => {
    const key = sourceKey(source);
    if (shownSources.has(key)) return false;
    shownSources.add(key);
    return true;
  });
  const issueGaps = new Set(result.issues.flatMap((issue) => issue.gaps));
  const additionalGaps = [...new Set(result.gaps)].filter(
    (gap) => !issueGaps.has(gap),
  );
  return (
    <div className="tea-legal-result">
      {showMode && (
        <p className="tea-legal-note">
          {result.assistant_mode === "live"
            ? "模型辅助分析 · 结论状态逐项列出"
            : "演示模式 · 未调用大模型"}
        </p>
      )}
      {result.issues.map((issue, index) => (
        <article key={issue.id} className="tea-legal-issue">
          <header>
            <span className="tea-legal-note">
              {issue.kind === "legal" ? "法律问题" : "信息问题"}
            </span>
            <span className="tea-conclusion-state" data-status={issue.status}>
              {conclusionLabels[issue.status]}
            </span>
            {issue.needs_review && (
              <span className="tea-legal-note">待复核</span>
            )}
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
          {(!detailsOnly || index >= 3) && (
            <p className="tea-legal-conclusion">{issue.conclusion}</p>
          )}
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
            <details className="tea-legal-gap-details">
              <summary>待补充与复核 · {new Set(issue.gaps).size} 项</summary>
              <ul>
                {[...new Set(issue.gaps)].map((gap, i) => (
                  <li key={i}>{gap}</li>
                ))}
              </ul>
            </details>
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
      {otherSources.length > 0 && (
        <section className="tea-node-other-sources" aria-label="其他原文依据">
          <h4>其他原文依据</h4>
          {otherSources.map((source) => (
            <CitationCard key={sourceKey(source)} citation={source} />
          ))}
        </section>
      )}
      {detailsOnly &&
        result.citations.length === 0 &&
        !result.issues.some((issue) => issue.citations.length > 0) && (
          <p className="tea-legal-note">
            本报告暂无直接引用的原文，可展开分析过程查看查找记录。
          </p>
        )}
      {!detailsOnly && !result.issues.length && result.output && (
        <p className="tea-legal-prose">{result.output}</p>
      )}
      {additionalGaps.length > 0 && (
        <details className="tea-legal-gap-details">
          <summary>其他待核对事项 · {additionalGaps.length} 项</summary>
          <ul>
            {additionalGaps.map((gap, i) => (
              <li key={i}>{gap}</li>
            ))}
          </ul>
        </details>
      )}
      {showTrace && <Trace steps={result.trace} />}
    </div>
  );
}
