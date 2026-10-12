"use client";

import { Plus, Save, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { errorMessage, teaRequest } from "@/core/tea/api";
import { countryLabel } from "@/core/tea/countries";
import type {
  DebugExpectation,
  Document,
  ListResult,
  StageDefinition,
} from "@/core/tea/types";
import type { WorkflowDebugger } from "@/core/tea/use-workflow-debugger";

import { ConfirmAction } from "./admin-shared";
import { ErrorNotice } from "./shared";

export function DebugSampleEditor({
  debug,
  stage,
  destination,
}: {
  debug: WorkflowDebugger;
  stage: StageDefinition;
  destination: string;
}) {
  const { sample, editSample } = debug;
  const locked = debug.running || debug.working;
  const currentValues = Object.fromEntries(
    Object.entries(sample.task_values).flatMap(([id, values]) => {
      const task = stage.tasks.find((item) => item.id === id);
      return task
        ? [
            [
              id,
              Object.fromEntries(
                Object.entries(values).filter(([key]) =>
                  task.fields.some((field) => field.key === key),
                ),
              ),
            ],
          ]
        : [];
    }),
  );
  const obsoleteValues = Object.entries(sample.task_values).flatMap(
    ([id, values]) =>
      !Object.hasOwn(currentValues, id) && !Object.keys(values).length
        ? ["已删除的空任务"]
        : Object.entries(values)
            .filter(([key]) => !Object.hasOwn(currentValues[id] ?? {}, key))
            .map(([, value]) => value),
  );
  const [documents, setDocuments] = useState<Document[]>([]);
  const [country, setCountry] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    teaRequest<ListResult<Document> & { country: string | null }>(
      `/templates/debug/documents?destination=${encodeURIComponent(destination)}&limit=50&offset=${offset}`,
      { signal: abort.signal },
    )
      .then((page) => {
        if (abort.signal.aborted) return;
        setCountry(page.country);
        setDocuments((items) =>
          offset
            ? [...items, ...page.items.filter((item) => !item.case_id)]
            : page.items.filter((item) => !item.case_id),
        );
        setTotal(page.total);
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [destination, offset, retry]);
  function updateExpectation(value: DebugExpectation) {
    editSample({
      expectations: sample.expectations.map((item) =>
        item.id === value.id ? value : item,
      ),
    });
  }
  function fieldValue(
    taskId: string,
    fieldKey: string,
    value: string | number,
  ) {
    const values = { ...sample.task_values[taskId] };
    if (value === "") delete values[fieldKey];
    else values[fieldKey] = value;
    editSample({ task_values: { ...sample.task_values, [taskId]: values } });
  }
  return (
    <div className="ta-debug-sample">
      <fieldset disabled={locked}>
        {obsoleteValues.length > 0 && (
          <div className="ta-debug-notice">
            <p>当前配置已移除 {obsoleteValues.length} 项测试输入。</p>
            <details>
              <summary>查看旧输入</summary>
              <p>{obsoleteValues.join("、")}</p>
            </details>
            <Button
              size="sm"
              variant="outline"
              onClick={() => editSample({ task_values: currentValues })}
            >
              移除旧输入
            </Button>
          </div>
        )}
        <label className="tea-field">
          <span>样例名称</span>
          <Input
            value={sample.name}
            maxLength={100}
            placeholder="例如：缺少企业名称"
            onChange={(event) => editSample({ name: event.target.value })}
          />
        </label>
        <label className="tea-field">
          <span>测试问题</span>
          <Textarea
            value={sample.input}
            rows={3}
            maxLength={20000}
            onChange={(event) => editSample({ input: event.target.value })}
          />
        </label>
        <details className="ta-debug-disclosure" open>
          <summary>填写资料</summary>
          {stage.tasks.map((task) => (
            <div className="ta-debug-task" key={task.id}>
              <strong>{task.title}</strong>
              {task.fields.length ? (
                task.fields.map((field) => (
                  <label className="tea-field" key={field.key}>
                    <span>
                      {field.label}
                      {field.required ? " *" : ""}
                    </span>
                    {field.type === "textarea" ? (
                      <Textarea
                        value={sample.task_values[task.id]?.[field.key] ?? ""}
                        rows={2}
                        maxLength={10000}
                        placeholder="留空可测试缺项"
                        onChange={(event) =>
                          fieldValue(task.id, field.key, event.target.value)
                        }
                      />
                    ) : (
                      <Input
                        type={field.type === "number" ? "number" : "text"}
                        value={sample.task_values[task.id]?.[field.key] ?? ""}
                        maxLength={10000}
                        placeholder="留空可测试缺项"
                        onChange={(event) =>
                          fieldValue(
                            task.id,
                            field.key,
                            field.type === "number" && event.target.value !== ""
                              ? Number(event.target.value)
                              : event.target.value,
                          )
                        }
                      />
                    )}
                  </label>
                ))
              ) : (
                <p className="ta-hint">此任务没有填写项。</p>
              )}
            </div>
          ))}
        </details>
        <details className="ta-debug-disclosure">
          <summary>
            检索资料 ·{" "}
            {sample.document_ids === null
              ? "目的地资料库"
              : `${sample.document_ids.length} 份`}
          </summary>
          <label className="tea-field">
            <span>资料范围</span>
            <select
              value={sample.document_ids === null ? "library" : "selected"}
              onChange={(event) =>
                editSample({
                  document_ids: event.target.value === "library" ? null : [],
                })
              }
            >
              <option value="library">目的地共有资料库</option>
              <option value="selected">指定资料（可留空）</option>
            </select>
          </label>
          <p className="ta-hint">
            {loading
              ? "正在读取资料…"
              : `范围：${countryLabel(country ?? "unassigned")}。 `}
            保存时固定资料版本；审核或索引变化后，重新保存可更新。留空可测试没有依据的情况。
          </p>
          {sample.document_ids !== null && (
            <div className="ta-debug-documents">
              {documents.map((item) => (
                <label key={item.id}>
                  <input
                    type="checkbox"
                    checked={sample.document_ids!.includes(item.id)}
                    disabled={
                      item.status !== "ready" &&
                      !sample.document_ids!.includes(item.id)
                    }
                    onChange={(event) =>
                      editSample({
                        document_ids: event.target.checked
                          ? [...sample.document_ids!, item.id]
                          : sample.document_ids!.filter((id) => id !== item.id),
                      })
                    }
                  />
                  <span>
                    {item.title || item.filename}
                    <small>
                      第 {item.version} 版
                      {item.status !== "ready" ? " · 尚不可用" : ""}
                    </small>
                  </span>
                </label>
              ))}
              {!documents.length && !loading && (
                <p className="ta-hint">暂无共有资料，可使用空资料样例。</p>
              )}
              {sample.document_ids.some(
                (id) => !documents.some((item) => item.id === id),
              ) && (
                <div>
                  <p className="ta-hint">
                    部分已选资料不在本页，可能属于旧的国家范围。运行前会校验。
                  </p>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => editSample({ document_ids: [] })}
                  >
                    清空资料选择
                  </Button>
                </div>
              )}
            </div>
          )}
          {error && (
            <ErrorNotice
              message={error}
              onRetry={() => setRetry((value) => value + 1)}
            />
          )}
          {offset + 50 < total && (
            <Button
              size="sm"
              variant="ghost"
              disabled={loading}
              onClick={() => setOffset((value) => value + 50)}
            >
              更多资料
            </Button>
          )}
        </details>
        <div className="ta-debug-section-heading">
          <strong>预期结果</strong>
          <Button
            size="sm"
            variant="ghost"
            disabled={sample.expectations.length >= 30}
            onClick={() =>
              editSample({
                expectations: [
                  ...sample.expectations,
                  { id: crypto.randomUUID(), kind: "manual", text: "" },
                ],
              })
            }
          >
            <Plus size={14} />
            添加
          </Button>
        </div>
        {!sample.expectations.length && (
          <p className="ta-hint">添加预期，才能判断修改是否有效。</p>
        )}
        {sample.expectations.map((item, index) => (
          <div className="ta-debug-expectation" key={item.id}>
            <div>
              <select
                aria-label={`预期 ${index + 1} 的检查方式`}
                value={item.kind}
                onChange={(event) =>
                  updateExpectation({
                    id: item.id,
                    kind: event.target.value as DebugExpectation["kind"],
                    ...(event.target.value === "check"
                      ? {
                          task_id: stage.tasks[0]?.id,
                          check_id:
                            stage.tasks[0]?.fields[0]?.key ?? "document",
                          status: "missing" as const,
                        }
                      : event.target.value === "citation"
                        ? {}
                        : { text: "" }),
                  })
                }
              >
                <option value="manual">人工确认</option>
                <option value="check">字段或材料检查</option>
                <option value="citation">应引用指定资料</option>
                <option value="contains">回答应包含文字</option>
                <option value="not_contains">回答不应包含文字</option>
              </select>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`删除预期 ${index + 1}`}
                onClick={() =>
                  editSample({
                    expectations: sample.expectations.filter(
                      (value) => value.id !== item.id,
                    ),
                  })
                }
              >
                <Trash2 size={14} />
              </Button>
            </div>
            {item.kind === "check" ? (
              <>
                <select
                  aria-label={`预期 ${index + 1} 的任务`}
                  value={item.task_id ?? ""}
                  onChange={(event) => {
                    const task = stage.tasks.find(
                      (value) => value.id === event.target.value,
                    );
                    updateExpectation({
                      ...item,
                      task_id: event.target.value,
                      check_id: task?.fields[0]?.key ?? "document",
                    });
                  }}
                >
                  {!stage.tasks.some((task) => task.id === item.task_id) && (
                    <option value={item.task_id ?? ""}>
                      原任务已删除，请重新选择
                    </option>
                  )}
                  {stage.tasks.map((task) => (
                    <option key={task.id} value={task.id}>
                      {task.title}
                    </option>
                  ))}
                </select>
                <div>
                  <select
                    aria-label={`预期 ${index + 1} 的填写项`}
                    value={item.check_id ?? ""}
                    onChange={(event) =>
                      updateExpectation({
                        ...item,
                        check_id: event.target.value,
                      })
                    }
                  >
                    {!["document", "confirmation"].includes(
                      item.check_id ?? "",
                    ) &&
                      !stage.tasks
                        .find((task) => task.id === item.task_id)
                        ?.fields.some(
                          (field) => field.key === item.check_id,
                        ) && (
                        <option value={item.check_id ?? ""}>
                          原填写项已删除，请重新选择
                        </option>
                      )}
                    {stage.tasks
                      .find((task) => task.id === item.task_id)
                      ?.fields.map((field) => (
                        <option key={field.key} value={field.key}>
                          {field.label}
                        </option>
                      ))}
                    <option value="document">关联材料</option>
                    <option value="confirmation">人工确认</option>
                  </select>
                  <select
                    aria-label={`预期 ${index + 1} 的状态`}
                    value={item.status ?? "missing"}
                    onChange={(event) =>
                      updateExpectation({
                        ...item,
                        status: event.target
                          .value as DebugExpectation["status"],
                      })
                    }
                  >
                    <option value="missing">待补充</option>
                    <option value="passed">通过</option>
                    <option value="conflict">有冲突</option>
                  </select>
                </div>
              </>
            ) : item.kind === "citation" ? (
              <select
                aria-label={`预期 ${index + 1} 的引用资料`}
                value={item.document_id ?? ""}
                onChange={(event) =>
                  updateExpectation({
                    ...item,
                    document_id: event.target.value,
                  })
                }
              >
                <option value="">选择资料</option>
                {documents.map((document) => (
                  <option value={document.id} key={document.id}>
                    {document.title} · 第 {document.version} 版
                  </option>
                ))}
              </select>
            ) : (
              <Textarea
                aria-label={`预期 ${index + 1} 的内容`}
                value={item.text ?? ""}
                rows={2}
                maxLength={2000}
                placeholder={
                  item.kind === "manual"
                    ? "例如：明确说明需要补充什么，建议清楚易懂"
                    : "输入需要逐字检查的文字"
                }
                onChange={(event) =>
                  updateExpectation({ ...item, text: event.target.value })
                }
              />
            )}
          </div>
        ))}
      </fieldset>
      <div className="ta-debug-save-actions">
        <Button
          size="sm"
          variant="outline"
          disabled={locked || (!debug.sampleDirty && !debug.sampleId)}
          onClick={() => void debug.saveSample()}
        >
          <Save size={14} />
          保存样例
        </Button>
        {debug.sampleId && (
          <Button
            size="sm"
            variant="ghost"
            disabled={locked}
            onClick={() => void debug.saveSample(true)}
          >
            另存为
          </Button>
        )}
        {debug.sampleId && (
          <ConfirmAction
            title="删除这份测试样例？"
            description="历史运行记录仍会保留。"
            disabled={locked}
            onConfirm={() => void debug.deleteSample()}
          >
            删除
          </ConfirmAction>
        )}
      </div>
    </div>
  );
}
