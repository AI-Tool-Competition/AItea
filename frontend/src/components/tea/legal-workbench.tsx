"use client";

import {
  BookOpen,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Ellipsis,
  FilePenLine,
  History,
  ListChecks,
  MessageSquarePlus,
  Plus,
  RefreshCw,
  Scale,
  Square,
} from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { makeConfigId } from "@/core/tea/configuration";
import {
  type CaseDetail,
  type LegalFact,
  type LegalFactInput,
  type LegalFactRequirement,
} from "@/core/tea/types";
import {
  legalFactInput,
  useLegalWorkbench,
} from "@/core/tea/use-legal-workbench";

import { ConfirmAction } from "./admin-shared";
import { LegalResultView, legalStatusLabel } from "./legal-result";
import {
  ActionButton,
  Busy,
  dateLabel,
  Empty,
  ErrorNotice,
  Trace,
} from "./shared";

const emptyFact = (requirement: LegalFactRequirement): LegalFact => ({
  key: requirement.key,
  label: requirement.label,
  value: null,
  source: "尚未提供",
  confirmed: false,
  extraction_confidence: "unknown",
  evidence_status: "unverified",
  evidence_refs: [],
  conflicts: [],
  revision: 0,
});
const confidenceLabels = {
  unknown: "未评估",
  low: "低",
  medium: "中",
  high: "高",
};

function FactEditor({
  fact,
  input,
  purpose,
  documents,
  busy,
  onEdit,
  onSave,
  drafted = false,
}: {
  fact: LegalFact;
  input: LegalFactInput;
  purpose?: string;
  documents: CaseDetail["documents"];
  busy: boolean;
  onEdit: (patch: Partial<LegalFactInput>) => void;
  onSave: () => void;
  drafted?: boolean;
}) {
  const conflict = input.expected_revision !== fact.revision;
  const editor = (
    <fieldset className="tea-legal-fact" disabled={busy}>
      <legend>
        {fact.label}
        {drafted && <span className="tea-node-field-state">待保存</span>}
      </legend>
      {purpose && <p className="tea-legal-note">{purpose}</p>}
      <label className="tea-field">
        <span className="sr-only">{fact.label}</span>
        <Input
          value={input.value ?? ""}
          maxLength={10000}
          onChange={(e) =>
            onEdit({
              value: e.target.value || null,
              confirmed: false,
              evidence_status: fact.conflicts.length
                ? "conflict"
                : "unverified",
              review_note: "",
            })
          }
        />
      </label>
      <label className="tea-checkbox">
        <input
          type="checkbox"
          checked={input.confirmed}
          disabled={
            input.value === null ||
            (typeof input.value === "string" && !input.value.trim())
          }
          onChange={(e) => onEdit({ confirmed: e.target.checked })}
        />
        我确认这项事实
      </label>
      <details>
        <summary>
          证据核验
          {input.evidence_status === "verified"
            ? " · 已核验"
            : input.evidence_status === "conflict"
              ? " · 有冲突"
              : " · 待核验"}
        </summary>
        <p className="tea-legal-note">
          来源：{fact.source} · 提取置信度：
          {confidenceLabels[fact.extraction_confidence]}
        </p>
        <p className="tea-legal-note">
          确认事实与核验证据分别记录。核验通过需要关联可用原件，并填写核验说明；冲突来源会保留。
        </p>
        {fact.conflicts.length > 0 && (
          <div className="tea-legal-conflict">
            <strong>存在不同说法</strong>
            {fact.conflicts.map((item, i) => (
              <p key={i}>
                {String(item.value ?? "未说明")} · {item.source}
                {item.evidence_refs.length
                  ? ` · 依据 ${item.evidence_refs.join("、")}`
                  : ""}
              </p>
            ))}
          </div>
        )}
        {!!fact.resolved_conflicts?.length && (
          <div className="tea-legal-conflict">
            <strong>已处理的冲突</strong>
            {fact.resolved_conflicts.map((item, i) => (
              <p key={i}>
                {String(item.value ?? "未说明")} · {item.source}
                {item.evidence_refs.length
                  ? ` · 依据 ${item.evidence_refs.join("、")}`
                  : ""}
              </p>
            ))}
            {fact.review_note && <p>处理说明：{fact.review_note}</p>}
          </div>
        )}
        <label className="tea-field">
          <span>证据状态</span>
          <select
            value={input.evidence_status}
            onChange={(e) =>
              onEdit({
                evidence_status: e.target
                  .value as LegalFactInput["evidence_status"],
              })
            }
          >
            <option value="unverified">待核验</option>
            <option value="verified">核验通过</option>
            <option value="conflict">有冲突</option>
          </select>
        </label>
        <div className="tea-legal-evidence">
          <strong>关联原件</strong>
          {documents
            .filter(
              (item) => item.role === "primary" && item.status === "ready",
            )
            .map((document) => (
              <label className="tea-checkbox" key={document.id}>
                <input
                  type="checkbox"
                  checked={input.evidence_refs.includes(document.id)}
                  onChange={(e) =>
                    onEdit({
                      evidence_refs: e.target.checked
                        ? [...input.evidence_refs, document.id]
                        : input.evidence_refs.filter(
                            (id) => id !== document.id,
                          ),
                    })
                  }
                />
                {document.title} · v{document.version}
              </label>
            ))}
          {input.evidence_refs
            .filter((id) => !documents.some((item) => item.id === id))
            .map((id) => (
              <label className="tea-checkbox" key={id}>
                <input
                  type="checkbox"
                  checked
                  onChange={() =>
                    onEdit({
                      evidence_refs: input.evidence_refs.filter(
                        (value) => value !== id,
                      ),
                    })
                  }
                />
                已关联资料 {id}
              </label>
            ))}
          {documents.length === 0 && input.evidence_refs.length === 0 && (
            <p className="tea-legal-note">
              当前业务尚无可关联的原件，请先上传资料。
            </p>
          )}
        </div>
        <label className="tea-field">
          <span>核验或冲突处理说明</span>
          <Textarea
            rows={2}
            value={input.review_note}
            maxLength={2000}
            onChange={(e) => onEdit({ review_note: e.target.value })}
          />
        </label>
      </details>
      {conflict && (
        <div className="tea-legal-conflict">
          <p>
            最新保存值：{String(fact.value ?? "未填写")}
            。核对后可将保留的输入用于新版本。
          </p>
          <Button
            size="sm"
            variant="outline"
            onClick={() => onEdit({ expected_revision: fact.revision })}
          >
            保留输入并采用最新修订号
          </Button>
        </div>
      )}
      <Button
        size="sm"
        variant="outline"
        aria-label={`保存事实：${fact.label}`}
        disabled={
          conflict ||
          (input.evidence_status === "verified" &&
            (!input.evidence_refs.length || !input.review_note.trim()))
        }
        onClick={onSave}
      >
        保存事实
      </Button>
    </fieldset>
  );
  return editor;
}
type WorkbenchProps = {
  accountId: string;
  record: CaseDetail;
  stageId: string;
  onUpdate?: (record: CaseDetail) => void;
  embedded?: boolean;
  analysisId?: string;
  analysisRevision?: number;
  readOnly?: boolean;
};
type NodeToolPanel =
  | "followups"
  | "evidence"
  | "report"
  | "facts"
  | "question"
  | "history";
function NodePanel({
  id,
  title,
  open,
  onClose,
  children,
}: {
  id: string;
  title: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      className="tea-node-tool-panel"
      hidden={!open}
      role="region"
      aria-labelledby={id + "-title"}
    >
      <header>
        <h5 id={id + "-title"}>{title}</h5>
        <Button
          variant="ghost"
          size="sm"
          aria-label={"收起" + title}
          onClick={onClose}
        >
          <ChevronUp size={14} />
          收起
        </Button>
      </header>
      {children}
    </section>
  );
}
function FactNavigator({
  label,
  choices,
  selected,
  onSelect,
}: {
  label: string;
  choices: readonly { key: string; label: string }[];
  selected: string;
  onSelect: (key: string) => void;
}) {
  if (choices.length < 2) return null;
  const index = Math.max(
    0,
    choices.findIndex((item) => item.key === selected),
  );
  return (
    <div className="tea-node-fact-navigation">
      <select
        aria-label={label}
        value={choices[index]!.key}
        onChange={(event) => onSelect(event.target.value)}
      >
        {choices.map((item, position) => (
          <option key={item.key} value={item.key}>
            {position + 1}. {item.label}
          </option>
        ))}
      </select>
      <div>
        <span>
          {index + 1} / {choices.length}
        </span>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="上一项信息"
          disabled={index === 0}
          onClick={() => onSelect(choices[index - 1]!.key)}
        >
          <ChevronLeft size={14} />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="下一项信息"
          disabled={index === choices.length - 1}
          onClick={() => onSelect(choices[index + 1]!.key)}
        >
          <ChevronRight size={14} />
        </Button>
      </div>
    </div>
  );
}
export function LegalWorkbench(props: WorkbenchProps) {
  return (
    <ScopedLegalWorkbench
      key={JSON.stringify([props.accountId, props.record.id, props.stageId])}
      {...props}
    />
  );
}
function ScopedLegalWorkbench({
  accountId,
  record,
  stageId,
  onUpdate,
  embedded = false,
  analysisId,
  analysisRevision,
  readOnly = false,
}: WorkbenchProps) {
  const legal = useLegalWorkbench(
    accountId,
    record.id,
    stageId,
    record.revision,
    onUpdate,
    analysisId,
    analysisRevision,
  );
  const [followupPage, setFollowupPage] = useState(0);
  const [activePanel, setActivePanel] = useState<NodeToolPanel | null>(null);
  const [selectedFollowupKey, setSelectedFollowupKey] = useState("");
  const [followupSnapshot, setFollowupSnapshot] = useState<
    LegalFactRequirement[]
  >([]);
  const [selectedFactKey, setSelectedFactKey] = useState("");
  const panelId = useId();
  const panelTriggers = useRef<
    Partial<Record<NodeToolPanel, HTMLButtonElement | null>>
  >({});
  const moreTrigger = useRef<HTMLButtonElement>(null);
  const [newLabel, setNewLabel] = useState("");
  const analysis = legal.analysis;
  const [versionsExpanded, setVersionsExpanded] = useState(false);
  const versionLoader = useRef(legal.loadVersions);
  versionLoader.current = legal.loadVersions;
  const versionAnalysisId = analysis?.id;
  const versionRevision = analysis?.revision;
  useEffect(() => {
    if (versionsExpanded && versionAnalysisId) void versionLoader.current();
  }, [versionsExpanded, versionAnalysisId, versionRevision]);
  const requirements = analysis?.result.followups ?? [];
  const maxQuestions = Math.min(
    5,
    Math.max(
      1,
      record.template.stages?.find((stage) => stage.id === stageId)?.analysis
        ?.legal?.question_limit ??
        record.template.agent.legal?.question_limit ??
        3,
    ),
  );
  const questionPage = Math.min(
    followupPage,
    Math.max(0, Math.ceil(requirements.length / maxQuestions) - 1),
  );
  const draftFacts = Object.entries(legal.draft.facts).map(([key, input]) => ({
    ...emptyFact({ key, label: input.label, purpose: "", decisive: false }),
    revision: input.expected_revision,
  }));
  const facts = new Map(
    [
      ...draftFacts,
      ...(analysis?.result.facts ?? []),
      ...legal.history.facts,
    ].map((fact) => [fact.key, fact]),
  );
  const currentFacts = [...facts.values()].filter(
    (fact) => !requirements.some((item) => item.key === fact.key),
  );
  const locked =
    record.stages.find((stage) => stage.id === stageId)?.status === "locked";
  const editable = !legal.busy && !locked && !readOnly;
  function renderFact(fact: LegalFact, purpose?: string) {
    return (
      <FactEditor
        key={fact.key}
        fact={fact}
        drafted={!!legal.draft.facts[fact.key]}
        purpose={purpose}
        input={legal.draft.facts[fact.key] ?? legalFactInput(fact)}
        documents={record.documents}
        busy={!editable}
        onEdit={(patch) => legal.editFact(fact, patch)}
        onSave={() => void legal.saveFact(fact)}
      />
    );
  }

  const currentRequirementKeys = new Set(requirements.map((item) => item.key));
  const editorRequirements = embedded
    ? [
        ...requirements,
        ...followupSnapshot.filter(
          (item) =>
            !currentRequirementKeys.has(item.key) &&
            (!!legal.draft.facts[item.key] ||
              (activePanel === "followups" &&
                item.key === selectedFollowupKey)),
        ),
      ]
    : requirements;
  const selectedFollowupIndex = Math.max(
    0,
    editorRequirements.findIndex((item) => item.key === selectedFollowupKey),
  );
  const selectedFactIndex = Math.max(
    0,
    currentFacts.findIndex((item) => item.key === selectedFactKey),
  );
  const factDraftCount = currentFacts.filter(
    (fact) => !!legal.draft.facts[fact.key],
  ).length;
  const reportDraftCount = Object.values(legal.draft.sections).filter(
    (item) => item.analysis_id === analysis?.id,
  ).length;
  const staleFactCount = [...facts.values()].filter((fact) => {
    const edit = legal.draft.facts[fact.key];
    return edit && edit.expected_revision !== fact.revision;
  }).length;
  const staleReportCount =
    analysis?.sections.filter((section) => {
      const edit = legal.sectionDraft(section);
      return edit && edit.expected_revision !== analysis.revision;
    }).length ?? 0;
  function togglePanel(panel: NodeToolPanel) {
    if (panel === "followups") {
      if (!selectedFollowupKey)
        setSelectedFollowupKey(requirements[0]?.key ?? "");
      setFollowupSnapshot((previous) => [
        ...new Map(
          [...previous, ...requirements].map((item) => [item.key, item]),
        ).values(),
      ]);
    }
    if (panel === "facts" && !selectedFactKey)
      setSelectedFactKey(currentFacts[0]?.key ?? "");
    setActivePanel((previous) => (previous === panel ? null : panel));
  }
  function closePanel() {
    const trigger =
      activePanel === "history" || activePanel === "question"
        ? moreTrigger.current
        : activePanel
          ? panelTriggers.current[activePanel]
          : null;
    setActivePanel(null);
    trigger?.focus();
  }
  const questionEditor = (
    <>
      {embedded && analysis && (
        <p className="tea-legal-note">本次范围：{analysis.question}</p>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void legal.run();
        }}
      >
        <label className="tea-field">
          <span>法律分析问题</span>
          <Textarea
            rows={3}
            maxLength={10000}
            value={legal.draft.question}
            onChange={(e) => legal.editQuestion(e.target.value)}
            placeholder="例如：这笔出口交易的付款条款有哪些风险？"
          />
        </label>
        <div className="tea-legal-toolbar">
          <ActionButton
            size="sm"
            type="submit"
            busy={legal.busy === "run"}
            disabled={
              !editable ||
              legal.loading ||
              !!legal.historyError ||
              !legal.draft.question.trim()
            }
          >
            开始分析
          </ActionButton>
          {legal.busy === "run" && (!embedded || !analysis) && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={legal.stop}
            >
              <Square size={13} />
              停止分析
            </Button>
          )}
        </div>
      </form>
    </>
  );
  const historyEditor = (
    <>
      <div className="tea-legal-history">
        {!embedded && <h4>本节点的分析记录</h4>}
        {legal.loading ? (
          <Busy label="读取分析记录…" />
        ) : legal.historyError ? (
          <ErrorNotice message={legal.historyError} onRetry={legal.reload} />
        ) : (
          <>
            {legal.history.analyses.length > 0 ? (
              <label className="tea-field">
                <span className="sr-only">选择分析记录</span>
                <select
                  value={
                    legal.history.analyses.some(
                      (item) => item.id === legal.draft.selectedId,
                    )
                      ? legal.draft.selectedId
                      : ""
                  }
                  disabled={!!legal.busy}
                  onChange={(e) => {
                    setFollowupPage(0);
                    legal.choose(e.target.value);
                  }}
                >
                  <option value="" disabled>
                    选择分析记录
                  </option>
                  {legal.history.analyses.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.question} · {legalStatusLabel[item.status]} ·{" "}
                      {dateLabel(item.created_at)}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <p className="tea-legal-note">
                还没有分析记录。写下具体问题即可开始。
              </p>
            )}
            {legal.history.total > 20 && (
              <nav className="tea-legal-pagination" aria-label="分析历史分页">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={legal.page === 0 || !!legal.busy}
                  onClick={() => legal.setPage(legal.page - 1)}
                >
                  上一页
                </Button>
                <span>
                  第 {legal.page + 1} 页 · 共 {legal.history.total} 条
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={
                    (legal.page + 1) * 20 >= legal.history.total || !!legal.busy
                  }
                  onClick={() => legal.setPage(legal.page + 1)}
                >
                  下一页
                </Button>
              </nav>
            )}
          </>
        )}
      </div>
    </>
  );
  const followupEditor = (
    <section className="tea-legal-followups" aria-label="需要补充的事实">
      {!embedded && <h4>先补充这些信息</h4>}
      <p className="tea-legal-note">
        保存事实后，可重新分析相关问题。
        {requirements.length > maxQuestions
          ? `每组最多 ${maxQuestions} 项。`
          : ""}
      </p>
      {embedded && (
        <FactNavigator
          label="选择要补充的信息"
          choices={editorRequirements}
          selected={editorRequirements[selectedFollowupIndex]?.key ?? ""}
          onSelect={setSelectedFollowupKey}
        />
      )}
      {embedded &&
        editorRequirements[selectedFollowupIndex] &&
        !currentRequirementKeys.has(
          editorRequirements[selectedFollowupIndex].key,
        ) && (
          <p role="status" className="tea-legal-note">
            这项信息已不在当前待补清单中，你的输入仍为你保留。
          </p>
        )}
      {editorRequirements
        .slice(
          embedded ? selectedFollowupIndex : questionPage * maxQuestions,
          embedded
            ? selectedFollowupIndex + 1
            : (questionPage + 1) * maxQuestions,
        )
        .map((requirement) =>
          renderFact(
            facts.get(requirement.key) ?? emptyFact(requirement),
            requirement.purpose,
          ),
        )}
      {!embedded && requirements.length > maxQuestions && (
        <nav className="tea-legal-pagination" aria-label="补充事实分组">
          <Button
            size="sm"
            variant="ghost"
            disabled={questionPage === 0}
            onClick={() => setFollowupPage(questionPage - 1)}
          >
            上一组问题
          </Button>
          <span>
            {questionPage + 1} / {Math.ceil(requirements.length / maxQuestions)}
          </span>
          <Button
            size="sm"
            variant="ghost"
            disabled={(questionPage + 1) * maxQuestions >= requirements.length}
            onClick={() => setFollowupPage(questionPage + 1)}
          >
            下一组问题
          </Button>
        </nav>
      )}
    </section>
  );
  const retainedEditor =
    legal.retainedSections.length > 0 ? (
      <section className="tea-legal-sections" aria-label="原争点未保存草稿">
        <h4>原争点未保存草稿</h4>
        <p className="tea-legal-note">
          原段落的争点或标题已改变，以下输入仍为你保留。点击文本框可全选，复制到合适的新段落后保存。
        </p>
        {legal.retainedSections.map(([key, edit]) => (
          <div className="tea-legal-section" key={key}>
            <label className="tea-field">
              <span>{edit.title}</span>
              <Textarea
                aria-label={`原争点草稿：${edit.title}`}
                rows={5}
                readOnly
                value={edit.content}
                onFocus={(e) => e.currentTarget.select()}
              />
            </label>
            <ConfirmAction
              title="丢弃这份未保存草稿？"
              description="此操作只移除保留的本地输入，已保存的报告不受影响。丢弃后无法恢复。"
              disabled={!editable}
              onConfirm={() => legal.discardSectionDraft(key)}
            >
              丢弃草稿：{edit.title}
            </ConfirmAction>
          </div>
        ))}
      </section>
    ) : null;
  const reportEditor = (
    <>
      {analysis && analysis.sections.length > 0 && (
        <section className="tea-legal-sections" aria-label="法律分析报告">
          {!embedded && <h4>分析报告</h4>}
          <p className="tea-legal-note">
            人工编辑的段落会保留。重新分析生成的建议需逐段确认。
          </p>
          {analysis.sections.map((section) => {
            const edit = legal.sectionDraft(section);
            const stale =
              !!edit && edit.expected_revision !== analysis.revision;
            return (
              <div className="tea-legal-section" key={section.id}>
                <label className="tea-field">
                  <span>
                    {section.title}
                    {section.edited ? " · 已人工编辑" : ""}
                    {section.needs_review ? " · 待复核" : ""}
                  </span>
                  <Textarea
                    aria-label={`报告段落：${section.title}`}
                    rows={6}
                    maxLength={30000}
                    value={edit?.content ?? section.content}
                    disabled={!editable}
                    onChange={(e) => legal.editSection(section, e.target.value)}
                  />
                </label>
                {stale && (
                  <div className="tea-legal-conflict">
                    <strong>已有新修订，你的输入已保留</strong>
                    <p className="tea-legal-prose">
                      最新保存内容：{section.content}
                    </p>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!editable}
                      onClick={() => legal.rebaseSection(section)}
                    >
                      核对后采用最新修订号
                    </Button>
                  </div>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  aria-label={`保存段落：${section.title}`}
                  disabled={!editable || !edit || stale}
                  onClick={() => void legal.saveSection(section)}
                >
                  保存段落
                </Button>
                {section.suggested_content !== null && (
                  <details
                    className="tea-legal-suggestion"
                    open={!embedded || undefined}
                  >
                    <summary>待确认的修改建议</summary>
                    <div className="tea-legal-diff">
                      <div>
                        <strong>当前保存内容</strong>
                        <del>{section.content}</del>
                      </div>
                      <div>
                        <strong>分析建议</strong>
                        <ins>{section.suggested_content}</ins>
                      </div>
                    </div>
                    {edit && (
                      <p className="tea-legal-note">
                        先保存并核对当前输入，再接受建议。
                      </p>
                    )}
                    <Button
                      size="sm"
                      variant="outline"
                      aria-label={`接受建议：${section.title}`}
                      disabled={!editable || !!edit}
                      onClick={() => void legal.saveSection(section, true)}
                    >
                      接受此段建议
                    </Button>
                  </details>
                )}
              </div>
            );
          })}
        </section>
      )}
      {retainedEditor}
      {analysis && (
        <details
          className="tea-legal-versions"
          open={versionsExpanded}
          onToggle={(event) => setVersionsExpanded(event.currentTarget.open)}
        >
          <summary>报告修订记录</summary>
          {legal.versionsLoading && <Busy />}
          {legal.versions && (
            <>
              <ul>
                {legal.versions.items.map((item) => (
                  <li key={item.revision}>
                    修订 {item.revision} · {dateLabel(item.created_at, true)}
                  </li>
                ))}
              </ul>
              <nav className="tea-legal-pagination" aria-label="修订历史分页">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={
                    legal.versionsLoading || legal.versions.offset === 0
                  }
                  onClick={() =>
                    void legal.loadVersions(
                      Math.max(0, legal.versions!.offset - 20),
                    )
                  }
                >
                  较新修订
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={
                    legal.versionsLoading ||
                    legal.versions.offset + 20 >= legal.versions.total
                  }
                  onClick={() =>
                    void legal.loadVersions(legal.versions!.offset + 20)
                  }
                >
                  较早修订
                </Button>
              </nav>
            </>
          )}
        </details>
      )}
    </>
  );
  const factsEditor = (
    <section className="tea-legal-facts" aria-label="业务法律事实">
      {!embedded && <h4>已记录的事实</h4>}
      {legal.history.facts_truncated && (
        <p role="status" className="tea-legal-gaps">
          事实数量超过当前分析上限；完整记录仍由服务端保留，请先整理事实。
        </p>
      )}
      {embedded && (
        <FactNavigator
          label="选择已记录的事实"
          choices={currentFacts}
          selected={currentFacts[selectedFactIndex]?.key ?? ""}
          onSelect={setSelectedFactKey}
        />
      )}
      {(embedded
        ? currentFacts.slice(selectedFactIndex, selectedFactIndex + 1)
        : currentFacts
      ).map((fact) => renderFact(fact))}
      {!currentFacts.length && !requirements.length && (
        <Empty title="尚未记录法律事实">
          先提出问题，分析会提示需要补充的信息。
        </Empty>
      )}
      <details>
        <summary>
          <Plus size={13} />
          补充其他事实
        </summary>
        <label className="tea-field">
          <span>事实名称</span>
          <Input
            value={newLabel}
            maxLength={200}
            onChange={(e) => setNewLabel(e.target.value)}
          />
        </label>
        <Button
          size="sm"
          variant="outline"
          disabled={!editable || !newLabel.trim()}
          onClick={() => {
            const fact = emptyFact({
              key: makeConfigId("fact"),
              label: newLabel.trim(),
              purpose: "",
              decisive: false,
            });
            legal.editFact(fact, {});
            setNewLabel("");
          }}
        >
          添加事实
        </Button>
      </details>
    </section>
  );
  const detailsEditor = analysis && (
    <LegalResultView
      result={analysis.result}
      detailsOnly={embedded}
      showTrace={
        !legal.steps.length &&
        (!embedded ||
          analysis.id !== analysisId ||
          analysis.revision !== analysisRevision)
      }
      showMode={!embedded}
    />
  );
  const feedback = (
    <>
      {legal.error && (
        <>
          <ErrorNotice message={legal.error} />
          <Button
            size="sm"
            variant="outline"
            disabled={!!legal.busy}
            onClick={legal.reload}
          >
            读取最新版本，保留输入
          </Button>
        </>
      )}
      {legal.detailLoading && <Busy label="读取分析详情…" />}
      {legal.label && legal.busy === "run" && (
        <p role="status" className="tea-legal-note">
          {legal.label}
        </p>
      )}
      <Trace steps={legal.steps} running={legal.busy === "run"} />
    </>
  );
  const analysisHeader = analysis && (
    <header className={embedded ? "tea-node-result-heading" : undefined}>
      <div>
        <h4>{embedded ? "分析结论" : analysis.question}</h4>
        <span className={embedded ? "tea-node-result-status" : "ta-badge"}>
          {legalStatusLabel[analysis.status]}
        </span>
        {!embedded && <small>修订 {analysis.revision}</small>}
      </div>
      {embedded && legal.busy === "run" ? (
        <Button size="sm" variant="ghost" onClick={legal.stop}>
          <Square size={13} />
          停止分析
        </Button>
      ) : (
        <Button
          size="sm"
          variant={embedded ? "ghost" : "outline"}
          aria-label="重新分析受影响问题"
          disabled={!editable || legal.detailLoading}
          onClick={() => void legal.run(true)}
        >
          <RefreshCw size={13} />
          {embedded ? "重新分析" : "重新分析受影响问题"}
        </Button>
      )}
    </header>
  );
  function tool(
    panel: NodeToolPanel,
    label: string,
    icon: ReactNode,
    count = 0,
  ) {
    return (
      <Button
        ref={(element) => {
          panelTriggers.current[panel] = element;
        }}
        id={panelId + "-" + panel + "-trigger"}
        type="button"
        variant="ghost"
        size="sm"
        className="tea-node-tool"
        aria-expanded={activePanel === panel}
        aria-controls={panelId + "-" + panel}
        onClick={() => togglePanel(panel)}
      >
        {icon}
        {label}
        {count > 0 && (
          <span className="tea-node-draft-count">{count} 项待保存</span>
        )}
      </Button>
    );
  }
  if (embedded)
    return (
      <section
        className="tea-legal-workbench tea-legal-embedded"
        aria-label="本节点分析与编辑"
      >
        {locked && (
          <p className="tea-legal-note">当前节点尚未解锁，可查看已有记录。</p>
        )}
        {feedback}
        {analysis && (
          <div className="tea-legal-analysis">
            {analysisHeader}
            {analysis.error && <ErrorNotice message={analysis.error} />}
            <LegalResultView result={analysis.result} compact />
          </div>
        )}
        {(requirements.length > 0 || activePanel === "followups") && (
          <section
            className="tea-node-followup-card"
            aria-labelledby={panelId + "-followup-title"}
          >
            <header>
              <div>
                <h5 id={panelId + "-followup-title"}>
                  {requirements.length
                    ? `还有 ${requirements.length} 项信息需要补充`
                    : "保留的补充信息"}
                </h5>
                <p>
                  {requirements.length
                    ? "补充后，可重新分析相关问题。"
                    : "当前没有待补项，未保存输入仍为你保留。"}
                </p>
              </div>
              <Button
                ref={(element) => {
                  panelTriggers.current.followups = element;
                }}
                id={panelId + "-followups-trigger"}
                variant={activePanel === "followups" ? "ghost" : "secondary"}
                size="sm"
                aria-label={
                  activePanel === "followups" ? "收起补充信息" : "补充信息"
                }
                aria-expanded={activePanel === "followups"}
                aria-controls={panelId + "-followups"}
                onClick={() => togglePanel("followups")}
              >
                {activePanel === "followups" ? (
                  <ChevronUp size={14} />
                ) : (
                  <Plus size={14} />
                )}
                {activePanel === "followups" ? "收起" : "补充信息"}
              </Button>
            </header>
            <ul className="tea-node-followup-list">
              {requirements.map((item) => {
                const edit = legal.draft.facts[item.key];
                const stale =
                  edit &&
                  edit.expected_revision !== facts.get(item.key)?.revision;
                return (
                  <li key={item.key}>
                    <span>{item.label}</span>
                    {edit && <small>{stale ? "版本已变更" : "待保存"}</small>}
                  </li>
                );
              })}
            </ul>
            <div
              id={panelId + "-followups"}
              hidden={activePanel !== "followups"}
              role="region"
              aria-label="补充信息"
              className="tea-node-followup-editor"
            >
              {followupEditor}
            </div>
          </section>
        )}
        {staleFactCount + staleReportCount > 0 && (
          <p role="status" className="tea-node-draft-notice">
            已有新版本，{staleFactCount + staleReportCount}{" "}
            项输入需要核对；草稿仍为你保留。
          </p>
        )}
        {legal.retainedSections.length > 0 && (
          <div className="tea-node-draft-notice" role="status">
            <span>
              有 {legal.retainedSections.length} 份原争点草稿为你保留。
            </span>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => togglePanel("report")}
            >
              查看保留草稿
            </Button>
          </div>
        )}
        <nav className="tea-node-tools" aria-label="分析辅助工具">
          {analysis && tool("evidence", "原文依据", <BookOpen size={14} />)}
          {analysis?.sections.length || legal.retainedSections.length
            ? tool(
                "report",
                "编辑报告",
                <FilePenLine size={14} />,
                reportDraftCount,
              )
            : null}
          {tool(
            "facts",
            "已记录事实",
            <ListChecks size={14} />,
            factDraftCount,
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                ref={moreTrigger}
                variant="ghost"
                size="sm"
                className="tea-node-tool"
                data-panel-open={
                  activePanel === "history" || activePanel === "question"
                }
                aria-label="更多分析操作"
              >
                <Ellipsis size={15} />
                更多
                {legal.draft.question.trim() && (
                  <span className="tea-node-draft-count">未提交</span>
                )}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="tea-node-tools-menu">
              {analysis && (
                <DropdownMenuLabel>
                  当前报告 · 修订 {analysis.revision}
                </DropdownMenuLabel>
              )}
              <DropdownMenuItem onSelect={() => togglePanel("question")}>
                <MessageSquarePlus size={14} />
                补充问题
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => togglePanel("history")}>
                <History size={14} />
                分析记录
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </nav>
        <NodePanel
          id={panelId + "-evidence"}
          title="判断依据与原文"
          open={activePanel === "evidence"}
          onClose={closePanel}
        >
          {detailsEditor}
        </NodePanel>
        <NodePanel
          id={panelId + "-report"}
          title="编辑分析报告"
          open={activePanel === "report"}
          onClose={closePanel}
        >
          {reportEditor}
        </NodePanel>
        <NodePanel
          id={panelId + "-facts"}
          title="已记录的事实"
          open={activePanel === "facts"}
          onClose={closePanel}
        >
          {factsEditor}
        </NodePanel>
        <NodePanel
          id={panelId + "-question"}
          title="补充分析问题"
          open={activePanel === "question"}
          onClose={closePanel}
        >
          {questionEditor}
        </NodePanel>
        <NodePanel
          id={panelId + "-history"}
          title="本节点的分析记录"
          open={activePanel === "history"}
          onClose={closePanel}
        >
          {historyEditor}
        </NodePanel>
      </section>
    );
  return (
    <section className="tea-legal-workbench" aria-label="法律分析工作台">
      <header className="tea-legal-heading">
        <Scale size={18} />
        <div>
          <h3>法律分析</h3>
          <p>围绕问题补充事实，逐项查看结论与依据。</p>
        </div>
      </header>
      {locked && (
        <p className="tea-legal-note">当前节点尚未解锁，可查看已有记录。</p>
      )}
      {questionEditor}
      {feedback}
      {historyEditor}
      {analysis && (
        <div className="tea-legal-analysis">
          {analysisHeader}
          {analysis.error && <ErrorNotice message={analysis.error} />}
          {requirements.length > 0 && followupEditor}
          {detailsEditor}
          {reportEditor}
        </div>
      )}
      {factsEditor}
    </section>
  );
}
