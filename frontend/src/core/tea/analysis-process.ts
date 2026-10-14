import { countryLabel } from "./countries";
import { conclusionLabels } from "./legal-labels";
import { type Citation, type TraceStep } from "./types";

export type ProcessStep = Omit<TraceStep, "status"> & {
  status: TraceStep["status"] | "pending";
};
export const processStepLabels: Record<string, string> = {
  qa01: "理解业务问题",
  qa02: "梳理事实与缺项",
  qa03: "确认适用规则",
  qa04: "查找与核对依据",
  qa05: "分析结论与期限",
  qa06: "整理报告与下一步",
};
export type ProcessFinding = {
  title: string;
  details?: string[];
  attention?: string[];
  source?: Citation;
};
export type ProcessSection = {
  title: string;
  items: ProcessFinding[];
  empty: string;
  kind?: "sources";
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
function records(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item) => item && typeof item === "object").map(record)
    : [];
}
function text(value: unknown) {
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : "";
}
function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(text).filter(Boolean) : [];
}
function countries(value: unknown) {
  return strings(value).map(countryLabel).join("、") || "待明确";
}
function requirements(value: unknown): ProcessFinding[] {
  return records(value).map((item) => ({
    title: text(item.label) || "待补充信息",
    details: [text(item.purpose)].filter(Boolean),
  }));
}

// Present only business fields here. The untouched trace remains available in
// technical records; its identifiers and fingerprints are not user findings.
export function presentProcessStep(
  step: ProcessStep,
  steps: readonly ProcessStep[],
) {
  const output = record(step.output_data);
  const sections: ProcessSection[] = [];
  let summary = "";
  const add = (
    title: string,
    items: ProcessFinding[],
    empty: string,
    kind?: ProcessSection["kind"],
  ) => sections.push({ title, items, empty, kind });
  const issueNames = new Map<string, string>();
  for (const source of steps) {
    for (const issue of records(record(source.output_data).issues)) {
      if (text(issue.id) && text(issue.question))
        issueNames.set(text(issue.id), text(issue.question));
    }
  }
  const issueTitle = (item: Record<string, unknown>, index: number) =>
    text(item.question) ||
    (issueNames.get(text(item.issue_id)) ?? `问题 ${index + 1}`);

  if (step.id === "qa01" && Array.isArray(output.issues)) {
    const issues = records(output.issues);
    summary = issues.length
      ? `已整理 ${issues.length} 个需要关注的问题。`
      : "暂未整理出具体问题，请补充业务描述。";
    add(
      "本次关注的问题",
      issues.map((item, index) => ({
        title: issueTitle(item, index),
        details: [
          strings(item.domains).join(" · "),
          ...strings(item.depends_on).map((id) => {
            const prior = issues.findIndex((issue) => issue.id === id);
            return prior >= 0
              ? `需结合第 ${prior + 1} 个问题的结果判断`
              : "需结合关联问题的结果判断";
          }),
        ].filter(Boolean),
      })),
      "暂无具体问题",
    );
  } else if (step.id === "qa02" && Array.isArray(output.facts)) {
    const facts = records(output.facts);
    const missing = requirements(output.followups);
    summary = `已整理 ${facts.length} 项业务信息${missing.length ? `，还有 ${missing.length} 项待补充` : ""}。`;
    if (missing.length) add("需要补充的信息", missing, "");
    add(
      "已记录的业务信息",
      facts.map((item) => ({
        title: text(item.label) || "业务信息",
        details: [
          text(item.value) || "尚未填写",
          `${item.confirmed === true ? "已人工确认" : "待人工确认"} · ${item.evidence_status === "verified" ? "依据已核验" : item.evidence_status === "conflict" ? "依据有冲突" : "依据待核验"}`,
        ],
        attention: records(item.conflicts).map(
          (conflict) =>
            `冲突记录：${text(conflict.value) || "需核对原始资料"}${text(conflict.source) ? `（${text(conflict.source)}）` : ""}`,
        ),
      })),
      "尚未记录业务信息",
    );
  } else if (step.id === "qa03" && Array.isArray(output.scopes)) {
    const scopes = records(output.scopes);
    const gaps = scopes.reduce(
      (sum, item) => sum + strings(item.gaps).length,
      0,
    );
    summary = `已梳理 ${scopes.length} 个问题的适用范围${gaps ? `，有 ${gaps} 项信息待核实` : ""}。`;
    add(
      "适用范围与待核实事项",
      scopes.map((item, index) => ({
        title: issueTitle(item, index),
        details: [
          `监管涉及地区：${countries(item.regulatory_jurisdictions)}`,
          `争议处理地区：${countries(item.forum_jurisdictions)}`,
          `合同适用法律地区：${countries(item.governing_law_jurisdictions)}`,
        ],
        attention: strings(item.gaps),
      })),
      "适用范围尚未明确",
    );
  } else if (step.id === "qa04" && Array.isArray(output.coverage)) {
    const coverage = records(output.coverage);
    const citations = records(output.citations);
    summary = citations.length
      ? `已有 ${citations.length} 处原文可供核对，本步留下 ${coverage.length} 条查找记录。`
      : "暂未找到可引用的原文依据，仍需补充或核对资料。";
    const searchStatus: Record<string, string> = {
      covered: "找到匹配原文",
      partial: "仅找到部分匹配原文",
      empty: "未找到匹配原文",
      failed: "本次查找未成功",
    };
    add(
      "依据查找情况",
      coverage.map((item, index) => ({
        title: text(item.domain) || issueTitle(item, index),
        details: [
          `${countries(item.jurisdictions)} · ${Object.hasOwn(searchStatus, text(item.status)) ? searchStatus[text(item.status)] : "查找结果待核实"}`,
          text(item.query),
        ].filter(Boolean),
        attention: [text(item.warning)].filter(Boolean),
      })),
      "本步没有查找记录",
    );
    if (citations.length)
      add(
        "可供核对的原文",
        citations.map((item) => ({
          title: text(item.title) || "参考资料",
          source:
            typeof item.document_id === "string" && item.document_id
              ? ({
                  ...item,
                  document_id: item.document_id,
                  title: text(item.title) || "参考资料",
                  excerpt: text(item.excerpt),
                  locator: record(item.locator),
                } as Citation)
              : undefined,
          details: [
            text(item.document_version)
              ? `资料版本：第 ${text(item.document_version)} 版`
              : "",
            text(item.excerpt),
          ].filter(Boolean),
        })),
        "暂无原文依据",
        "sources",
      );
  } else if (step.id === "qa05" && Array.isArray(output.issues)) {
    const issues = records(output.issues);
    const unresolved = issues.filter(
      (item) => item.needs_review === true || item.status === "undetermined",
    ).length;
    summary = `已整理 ${issues.length} 项分析意见${unresolved ? `，其中 ${unresolved} 项仍需补充或复核` : ""}。`;
    add(
      "分析意见",
      issues.map((item, index) => ({
        title: issueTitle(item, index),
        details: [
          Object.hasOwn(conclusionLabels, text(item.status))
            ? conclusionLabels[
                text(item.status) as keyof typeof conclusionLabels
              ]
            : "结论待核实",
          text(item.conclusion),
          ...records(item.deadlines).map((deadline) =>
            deadline.status === "calculated" && text(deadline.date)
              ? `${text(deadline.label) || "办理期限"}：${text(deadline.date)}${text(deadline.timezone) ? `（${text(deadline.timezone)}）` : ""}`
              : `${text(deadline.label) || "办理期限"}：尚不能确定日期`,
          ),
        ].filter(Boolean),
        attention: [
          ...(item.needs_review === true ? ["本项仍需复核"] : []),
          ...requirements(item.missing_facts).map(
            (fact) => `待补充：${fact.title}`,
          ),
          ...strings(item.gaps),
        ],
      })),
      "暂未形成分析意见",
    );
  } else if (step.id === "qa06" && Array.isArray(output.sections)) {
    const report = records(output.sections);
    const missing = requirements(output.followups);
    summary = `已整理 ${report.length} 个报告段落${missing.length ? `，还有 ${missing.length} 项信息待补充` : ""}。`;
    if (missing.length) add("下一步需要补充", missing, "");
    add(
      "报告内容",
      report.map((item, index) => ({
        title: text(item.title) || `报告段落 ${index + 1}`,
      })),
      "暂未形成报告内容",
    );
  }

  // Preserve specific service errors and meaningful legacy descriptions. Only
  // routine implementation details are replaced by the structured summary.
  const routine =
    /^(本阶段已调用模型 \d+ 次并完成程序核对。|已执行实际检索或固定程序检查，未在本步骤调用模型。)$/.test(
      step.detail,
    );
  const finished = step.status === "completed" || step.status === "cached";
  const description = finished
    ? routine
      ? summary || "本步已完成，暂无可展示的摘要。"
      : step.detail || summary
    : step.detail;
  const input = record(step.input_data);
  const context: string[] = [];
  if (Array.isArray(input.jurisdictions))
    context.push(`涉及地区：${countries(input.jurisdictions)}`);
  if (Object.hasOwn(input, "document_ids"))
    context.push(
      Array.isArray(input.document_ids)
        ? `限定资料：${input.document_ids.length} 份`
        : "资料范围：当前可用资料",
    );
  return {
    description,
    sections,
    context,
    warning: text(output.warning),
  };
}
