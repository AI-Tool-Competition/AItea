"use client";

import {
  CheckCircle2,
  Download,
  Ellipsis,
  FileCheck2,
  FlaskConical,
  Save,
  Upload,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { errorMessage, TeaApiError, teaRequest } from "@/core/tea/api";
import {
  configurationIssueLabel,
  configurationSchema,
} from "@/core/tea/configuration";
import {
  type Template,
  type TemplateDraft,
  type TemplateDraftSummary,
  type ListResult,
  type TraceStep,
  type ValidationResult,
} from "@/core/tea/types";
import {
  clearConfigurationDrafts,
  importConfigurationDraft,
  localConfigurationDrafts,
  useConfigurationDraft,
} from "@/core/tea/use-configuration-draft";

import { ConfirmAction } from "./admin-shared";
import { AnalysisEditor } from "./analysis-editor";
import { ConfigurationDebugger } from "./configuration-debugger";
import { ActionButton, Busy, ErrorNotice } from "./shared";
import { WorkflowEditor } from "./workflow-editor";

type EditorPage = "workflow" | "analysis";
const selectedTemplates = new Map<string, string>();
export function clearStudioDrafts() {
  selectedTemplates.clear();
  clearConfigurationDrafts();
}

export function Studio({
  templates,
  ownerId,
  onPublished,
  page = "workflow",
  onPage,
  requestedStage,
}: {
  templates: Template[];
  ownerId: string;
  onPublished: (template: Template) => void;
  page?: EditorPage;
  onPage?: (page: EditorPage, stageId?: string) => void;
  requestedStage?: string;
}) {
  const [templateId, setTemplateId] = useState(
    selectedTemplates.get(ownerId) ?? templates[0]?.id ?? "",
  );
  const [localTemplates, setLocalTemplates] = useState(() =>
    localConfigurationDrafts(ownerId),
  );
  const [summaries, setSummaries] = useState<TemplateDraftSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [listError, setListError] = useState("");
  const [listLoading, setListLoading] = useState(true);
  const [selectionError, setSelectionError] = useState("");
  const [retry, setRetry] = useState(0);
  const available = new Map(
    [...templates, ...localTemplates].map((item) => [item.id, item]),
  );
  const options = new Map(
    [...templates, ...summaries, ...localTemplates].map((item) => [
      item.id,
      item,
    ]),
  );
  const template = available.get(templateId);
  useEffect(() => {
    const abort = new AbortController();
    setListLoading(true);
    setListError("");
    teaRequest<ListResult<TemplateDraftSummary>>(
      `/templates/drafts?limit=50&offset=${offset}`,
      { signal: abort.signal },
    )
      .then((result) => {
        if (abort.signal.aborted) return;
        setSummaries((current) =>
          offset === 0
            ? result.items
            : [
                ...new Map(
                  [...current, ...result.items].map((item) => [item.id, item]),
                ).values(),
              ],
        );
        setTotal(result.total);
      })
      .catch((error) => {
        if (!abort.signal.aborted) setListError(errorMessage(error));
      })
      .finally(() => {
        if (!abort.signal.aborted) setListLoading(false);
      });
    return () => abort.abort();
  }, [ownerId, offset, retry]);
  useEffect(() => {
    if (template || !templateId) return;
    const abort = new AbortController();
    setSelectionError("");
    teaRequest<TemplateDraft>(
      `/templates/${encodeURIComponent(templateId)}/draft`,
      { signal: abort.signal },
    )
      .then((result) => {
        if (abort.signal.aborted) return;
        if (!result.template)
          throw new Error("未找到这份草稿，请选择其他配置。");
        const loaded = configurationSchema.parse(result.template);
        setLocalTemplates((current) => [
          ...current.filter((item) => item.id !== loaded.id),
          loaded,
        ]);
      })
      .catch((error) => {
        if (!abort.signal.aborted) setSelectionError(errorMessage(error));
      });
    return () => abort.abort();
  }, [template, templateId, ownerId, retry]);
  function onImported(imported: Template, remote: TemplateDraft | null) {
    const next = {
      ...imported,
      version:
        templates.find((item) => item.id === imported.id)?.version ??
        remote?.template?.version ??
        1,
    };
    importConfigurationDraft(ownerId, next, remote);
    setLocalTemplates((current) => [
      ...current.filter((item) => item.id !== next.id),
      next,
    ]);
    selectedTemplates.set(ownerId, next.id);
    setTemplateId(next.id);
  }
  return (
    <div className="ta-studio">
      <div className="ta-template-select">
        <label>
          <span>当前业务配置</span>
          <select
            aria-label="当前业务配置"
            value={templateId}
            onChange={(e) => {
              selectedTemplates.set(ownerId, e.target.value);
              setTemplateId(e.target.value);
            }}
          >
            {[...options.values()].map((item) => (
              <option value={item.id} key={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        </label>
        {offset + 50 < total && (
          <Button
            size="sm"
            variant="ghost"
            disabled={listLoading}
            onClick={() => setOffset((value) => value + 50)}
          >
            更多草稿
          </Button>
        )}
      </div>
      {listError && (
        <ErrorNotice
          message={listError}
          onRetry={() => setRetry((value) => value + 1)}
        />
      )}
      {template ? (
        <EditorSession
          key={`${ownerId}:${template.id}`}
          initial={template}
          ownerId={ownerId}
          onPublished={onPublished}
          onImported={onImported}
          page={page}
          onPage={onPage}
          requestedStage={requestedStage}
        />
      ) : selectionError ? (
        <ErrorNotice
          message={selectionError}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : (
        <Busy label="读取业务配置…" />
      )}
    </div>
  );
}

function EditorSession({
  initial,
  ownerId,
  onPublished,
  onImported,
  page,
  onPage,
  requestedStage,
}: {
  initial: Template;
  ownerId: string;
  onPublished: (template: Template) => void;
  onImported: (template: Template, remote: TemplateDraft | null) => void;
  page: EditorPage;
  onPage?: (page: EditorPage, stageId?: string) => void;
  requestedStage?: string;
}) {
  const configuration = useConfigurationDraft(ownerId, initial);
  const { draft } = configuration;
  const [stageId, setStageId] = useState(
    requestedStage ?? draft.stages[0]?.id ?? "",
  );
  const [stepId, setStepId] = useState("");
  const [editingDefault, setEditingDefault] = useState(false);
  const [trace, setTrace] = useState<TraceStep[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [validation, setValidation] = useState<ValidationResult>();
  const [debugOpen, setDebugOpen] = useState(false);
  const debugPanel = useRef<HTMLDivElement>(null);
  const debugButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (debugOpen) debugPanel.current?.focus();
  }, [debugOpen]);
  function closeDebug() {
    setDebugOpen(false);
    debugButton.current?.focus();
  }
  const importRef = useRef<HTMLInputElement>(null);
  const pending = useRef<AbortController | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      pending.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (requestedStage) setStageId(requestedStage);
  }, [requestedStage]);
  const selected =
    draft.stages.find((stage) => stage.id === stageId) ?? draft.stages[0]!;
  const analysis = editingDefault
    ? draft.agent
    : (selected.analysis ?? draft.agent);
  const selectedStep =
    analysis.steps.find((step) => step.id === stepId) ?? analysis.steps[0];
  function update(next: Template) {
    configuration.update(next);
    setValidation(undefined);
    setNotice("");
    setTrace([]);
  }
  function selectStage(id: string) {
    setStageId(id);
    setStepId("");
    setTrace([]);
  }
  async function act(action: "save" | "validate" | "publish") {
    if (pending.current) return;
    const abort = new AbortController();
    pending.current = abort;
    setBusy(action);
    setError("");
    setNotice("");
    try {
      if (action === "save") {
        await configuration.save();
        return;
      }
      const checked = await teaRequest<ValidationResult>(
        "/templates/validate",
        {
          method: "POST",
          body: JSON.stringify({ template: draft }),
          signal: abort.signal,
        },
      );
      if (!alive.current) return;
      setValidation(checked);
      if (!checked.valid || action === "validate") return;
      const saved = await configuration.save();
      if (!alive.current) return;
      const published = await teaRequest<Template>("/templates/publish", {
        method: "POST",
        signal: abort.signal,
        body: JSON.stringify({
          template: draft,
          expected_draft_revision: saved.revision,
        }),
      });
      if (!alive.current) return;
      configuration.published(published, saved.revision + 1);
      onPublished(published);
      setValidation(undefined);
      setTrace([]);
      setNotice(
        `已发布第 ${published.version} 版。新建业务会读取这份配置，已有业务保留原版本。`,
      );
    } catch (e) {
      if (alive.current && !abort.signal.aborted) setError(errorMessage(e));
    } finally {
      if (alive.current) setBusy("");
      pending.current = null;
    }
  }
  function exportConfig() {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(draft, null, 2)], { type: "application/json" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${draft.id}-v${draft.version}-draft.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function importConfig(file?: File) {
    if (!file || pending.current) return;
    setError("");
    if (file.size > 1024 * 1024) {
      setError("配置文件不能超过 1 MB。");
      return;
    }
    const abort = new AbortController();
    pending.current = abort;
    setBusy("import");
    try {
      const result = configurationSchema.safeParse(
        JSON.parse(await file.text()),
      );
      if (!alive.current) return;
      if (!result.success)
        throw new Error(
          "无法读取配置，请检查文件结构。" +
            result.error.issues
              .slice(0, 3)
              .map((issue) => issue.path.join("."))
              .join("、"),
        );
      if (result.data.id !== draft.id) {
        let remote: TemplateDraft | null = null;
        try {
          remote = await teaRequest<TemplateDraft>(
            `/templates/${encodeURIComponent(result.data.id)}/draft`,
            { signal: abort.signal },
          );
        } catch (e) {
          if (!(e instanceof TeaApiError && e.status === 404)) throw e;
        }
        if (alive.current && !abort.signal.aborted)
          onImported(result.data, remote);
        return;
      }
      update({ ...result.data, version: draft.version });
      selectStage(result.data.stages[0]!.id);
      setNotice("配置已导入当前草稿。请校验、试运行，再发布。");
    } catch (e) {
      if (alive.current && !abort.signal.aborted) setError(errorMessage(e));
    } finally {
      if (alive.current) setBusy("");
      pending.current = null;
    }
  }
  const debugDraft =
    page === "analysis" && editingDefault
      ? {
          ...draft,
          stages: draft.stages.map((stage) =>
            stage.id === selected.id ? { ...stage, analysis: null } : stage,
          ),
        }
      : draft;
  return (
    <>
      <div className="ta-draft-bar">
        {(configuration.loading || configuration.dirty) && (
          <div className="ta-draft-status" role="status">
            <span
              className={`ta-dot ${configuration.dirty ? "unsaved" : ""}`}
            />
            {configuration.loading ? "读取中" : "未保存"}
          </div>
        )}
        <div className="ta-toolbar">
          <Button
            ref={debugButton}
            variant="outline"
            size="sm"
            disabled={!!busy || configuration.loading}
            aria-expanded={debugOpen}
            aria-controls="configuration-debug-drawer"
            onClick={() => setDebugOpen(!debugOpen)}
          >
            <FlaskConical size={14} />
            调试
          </Button>
          <ActionButton
            variant="outline"
            size="sm"
            busy={busy === "save"}
            disabled={!!busy || configuration.loading}
            onClick={() => act("save")}
          >
            <Save size={14} />
            保存草稿
          </ActionButton>
          <ActionButton
            size="sm"
            busy={busy === "publish"}
            disabled={!!busy || configuration.loading}
            onClick={() => act("publish")}
          >
            发布配置
          </ActionButton>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="更多配置操作"
                disabled={!!busy || configuration.loading}
              >
                <Ellipsis size={18} />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent className="tea-theme" align="end">
              <DropdownMenuItem
                onSelect={() => {
                  void act("validate");
                }}
              >
                <FileCheck2 size={14} />
                校验配置
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => importRef.current?.click()}>
                <Upload size={14} />
                导入配置
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={exportConfig}>
                <Download size={14} />
                导出配置
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
      <input
        hidden
        type="file"
        accept=".json,application/json"
        ref={importRef}
        onChange={(e) => {
          void importConfig(e.target.files?.[0]);
          e.target.value = "";
        }}
      />
      <div className="ta-studio-feedback">
        {configuration.loadError && (
          <ErrorNotice
            message={configuration.loadError}
            onRetry={configuration.retryLoad}
          />
        )}
        {error && <ErrorNotice message={error} />}
        {(configuration.loadError || error) && (
          <div className="ta-recovery">
            <Button variant="outline" size="sm" onClick={exportConfig}>
              导出当前草稿备份
            </Button>
            <ConfirmAction
              title="读取已保存草稿？"
              description="当前未保存的修改将被替换。需要保留时，请先导出备份。"
              onConfirm={() => {
                configuration.readSaved();
                setError("");
                setValidation(undefined);
                setTrace([]);
              }}
            >
              读取已保存草稿
            </ConfirmAction>
          </div>
        )}
        {notice && (
          <div className="tea-success" role="status">
            <CheckCircle2 size={16} />
            {notice}
            <Button
              className="ta-notice-dismiss"
              size="icon-sm"
              variant="ghost"
              aria-label="关闭提示"
              onClick={() => setNotice("")}
            >
              <X size={14} />
            </Button>
          </div>
        )}
        {validation && (
          <div
            className={
              validation.valid ? "ta-validation-ok" : "ta-validation-errors"
            }
            role="status"
          >
            {validation.valid ? (
              <>
                <CheckCircle2 size={16} />
                配置校验通过 · {validation.preview.stages} 个办理节点，
                {validation.preview.tasks} 项任务
              </>
            ) : (
              <>
                <strong>有 {validation.errors.length} 处需要调整</strong>
                <ul>
                  {validation.errors.map((item, i) => (
                    <li key={i}>
                      <button
                        onClick={() => {
                          const match = /stages\.(\d+)/.exec(item.path);
                          if (match && draft.stages[Number(match[1])])
                            selectStage(draft.stages[Number(match[1])]!.id);
                        }}
                      >
                        {configurationIssueLabel(item.path, draft)}
                      </button>
                      <span>{readableError(item.message)}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
      </div>
      {configuration.loading ? (
        <Busy label="读取已保存的配置草稿…" />
      ) : (
        <fieldset className="ta-editor-fieldset" disabled={!!busy}>
          {page === "workflow" ? (
            <WorkflowEditor
              draft={draft}
              onChange={update}
              stageId={selected.id}
              onStage={selectStage}
              onAnalyze={() => onPage?.("analysis", selected.id)}
            />
          ) : (
            <AnalysisEditor
              draft={draft}
              onChange={update}
              stageId={selected.id}
              onStage={selectStage}
              stepId={selectedStep?.id ?? ""}
              onStep={setStepId}
              trace={trace}
              editingDefault={editingDefault}
              onEditingDefault={(value) => {
                setEditingDefault(value);
                setTrace([]);
              }}
            />
          )}
          <div
            id="configuration-debug-drawer"
            className="ta-debug-drawer"
            role="region"
            aria-label="配置调试抽屉"
            hidden={!debugOpen}
            tabIndex={-1}
            ref={debugPanel}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.stopPropagation();
                closeDebug();
              }
            }}
          >
            <Button
              className="ta-debug-close"
              size="icon-sm"
              variant="ghost"
              aria-label="关闭调试"
              onClick={closeDebug}
            >
              <X size={18} />
            </Button>
            {editingDefault && page === "analysis" && (
              <p className="ta-hint ta-debug-scope-note">
                正在测试默认分析流程，使用「{selected.name}」的样例任务。
              </p>
            )}
            <ConfigurationDebugger
              key={`${selected.id}:${editingDefault && page === "analysis" ? "default" : "stage"}`}
              draft={debugDraft}
              stageId={selected.id}
              stepId={page === "analysis" ? selectedStep?.id : undefined}
              onTrace={setTrace}
            />
          </div>
        </fieldset>
      )}
    </>
  );
}
function readableError(message: string) {
  if (message.includes("at least 1 character")) return "请填写此项。";
  if (message.includes("at least 1 item")) return "请至少添加一项。";
  if (message.includes("String should match pattern"))
    return "编号须以小写字母开头，只包含字母、数字或下划线。";
  if (message.includes("greater than or equal"))
    return "数值低于允许的最小值。";
  if (message.includes("less than or equal")) return "数值超过允许的最大值。";
  if (message.startsWith("Value error, ")) return message.slice(13);
  return message;
}
