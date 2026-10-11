"use client";
import {
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  Download,
  FlaskConical,
  Plus,
  Save,
  Settings2,
  Upload,
} from "lucide-react";
import { useRef, useState } from "react";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { errorMessage, post } from "@/core/tea/api";
import {
  type DebugResult,
  type StageDefinition,
  type TaskDefinition,
  type Template,
  type ValidationResult,
} from "@/core/tea/types";

import {
  ActionButton,
  Checks,
  ErrorNotice,
  SectionHeading,
  Trace,
} from "./shared";
const fieldSchema = z
  .object({
    key: z.string(),
    label: z.string(),
    type: z.enum(["text", "number", "textarea"]),
    required: z.boolean(),
    min: z
      .number()
      .nullish()
      .transform((value) => value ?? undefined),
    placeholder: z
      .string()
      .nullish()
      .transform((value) => value ?? undefined),
  })
  .strict();
const taskSchema = z
  .object({
    id: z.string(),
    title: z.string(),
    description: z.string(),
    required: z.boolean(),
    fields: z.array(fieldSchema),
    require_document: z.boolean(),
    require_confirmation: z.boolean(),
    output_template: z
      .string()
      .nullish()
      .transform((value) => value ?? undefined),
  })
  .strict();
const schema = z
  .object({
    id: z.string(),
    version: z.number(),
    title: z.string(),
    description: z.string(),
    origin: z.string(),
    destination: z.string(),
    is_demo: z.boolean(),
    modes: z.array(
      z
        .object({
          id: z.string(),
          name: z.string(),
          description: z.string(),
          enabled: z.boolean(),
        })
        .strict(),
    ),
    stages: z
      .array(
        z
          .object({
            id: z.string(),
            name: z.string(),
            actor: z.string(),
            description: z.string(),
            tasks: z.array(taskSchema),
          })
          .strict(),
      )
      .min(1),
    agent: z
      .object({
        language: z.string(),
        instructions: z.string(),
        retrieval_limit: z.number(),
        steps: z.array(
          z
            .object({
              id: z.string(),
              type: z.enum(["retrieve", "check", "guide", "draft", "verify"]),
              label: z.string(),
              enabled: z.boolean(),
            })
            .strict(),
        ),
      })
      .strict(),
  })
  .strict();
const makeId = (prefix: string) =>
  `${prefix}_${crypto.randomUUID().slice(0, 8)}`;

const studioDrafts = new Map<string, Template>();
export function clearStudioDrafts() {
  studioDrafts.clear();
}

export function Studio({
  templates,
  ownerId,
  onPublished,
}: {
  templates: Template[];
  ownerId: string;
  onPublished: (template: Template) => void;
}) {
  const [draft, setDraft] = useState<Template | undefined>(
    templates[0]
      ? structuredClone(
          studioDrafts.get(`${ownerId}:${templates[0].id}`) ?? templates[0],
        )
      : undefined,
  );
  const [tab, setTab] = useState<"workflow" | "agent" | "debug">("workflow");
  const [stageIndex, setStageIndex] = useState(0);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [validation, setValidation] = useState<ValidationResult>();
  const [debugInput, setDebugInput] = useState(
    "我已准备好产品资料，请帮我检查还缺少哪些信息。",
  );
  const [debug, setDebug] = useState<DebugResult>();
  const importRef = useRef<HTMLInputElement>(null);
  const pending = useRef(false);
  if (!draft)
    return (
      <div className="tea-page">
        <ErrorNotice message="没有可编辑的业务模板。请先配置初始模板。" />
      </div>
    );
  const selected = draft.stages[stageIndex];
  function update(next: Template) {
    studioDrafts.set(`${ownerId}:${next.id}`, next);
    setDraft(next);
    setValidation(undefined);
    setNotice("");
  }
  function updateStage(next: StageDefinition) {
    if (!draft) return;
    update({
      ...draft,
      stages: draft.stages.map((stage, i) => (i === stageIndex ? next : stage)),
    });
  }
  function updateTask(index: number, next: TaskDefinition) {
    if (!selected) return;
    updateStage({
      ...selected,
      tasks: selected.tasks.map((task, i) => (i === index ? next : task)),
    });
  }
  async function run(action: "validate" | "publish" | "debug") {
    if (!draft || pending.current) return;
    pending.current = true;
    setBusy(action);
    setError("");
    setNotice("");
    try {
      if (action === "validate")
        setValidation(
          await post<ValidationResult>("/templates/validate", {
            template: draft,
          }),
        );
      else if (action === "publish") {
        const published = await post<Template>("/templates/publish", {
          template: draft,
        });
        studioDrafts.delete(`${ownerId}:${published.id}`);
        setDraft(published);
        onPublished(published);
        setNotice(
          `第 ${published.version} 版已发布，新建业务将使用这一版本。已有业务保持不变。`,
        );
      } else
        setDebug(
          await post<DebugResult>("/templates/debug", {
            template: draft,
            input: debugInput,
          }),
        );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
      pending.current = false;
    }
  }
  function exportConfig() {
    if (!draft) return;
    const blob = new Blob([JSON.stringify(draft, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${draft.id}-v${draft.version}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function importConfig(file?: File) {
    if (!file || pending.current) return;
    setError("");
    if (file.size > 1024 * 1024) {
      setError("配置文件不能超过 1 MB。");
      return;
    }
    try {
      const result = schema.safeParse(JSON.parse(await file.text()));
      if (!result.success)
        throw new Error(
          result.error.issues
            .map((issue) => `${issue.path.join(".")}：${issue.message}`)
            .join("；"),
        );
      update(result.data);
      setStageIndex(0);
      setNotice("配置已导入草稿，请校验并发布后使用。");
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <fieldset
      className="tea-page tea-studio"
      disabled={!!busy}
      style={{ minWidth: 0 }}
    >
      <SectionHeading
        eyebrow="DESIGNED BY EXPERTS"
        title="把专业经验，变成办理路径。"
      >
        <div className="tea-heading-actions">
          <Button
            variant="outline"
            size="sm"
            onClick={() => importRef.current?.click()}
          >
            <Upload size={14} />
            导入
          </Button>
          <Button variant="outline" size="sm" onClick={exportConfig}>
            <Download size={14} />
            导出
          </Button>
          <ActionButton
            busy={busy === "publish"}
            disabled={!!busy}
            onClick={() => run("publish")}
          >
            <Save size={14} />
            发布版本
          </ActionButton>
        </div>
      </SectionHeading>
      <p className="tea-page-intro">
        用表单编排主体、任务与核验条件。先调试，再发布；每次业务都有自己的版本。
      </p>
      <input
        type="file"
        accept=".json"
        ref={importRef}
        hidden
        onChange={(e) => {
          void importConfig(e.target.files?.[0]);
          e.target.value = "";
        }}
      />
      {error && <ErrorNotice message={error} />}
      {notice && (
        <div className="tea-success" role="status">
          <CheckCircle2 size={16} />
          {notice}
        </div>
      )}
      <label className="tea-field">
        <span>选择业务模板 · 未发布修改会在当前工作台保留</span>
        <select
          value={draft.id}
          onChange={(e) => {
            const selected = templates.find((t) => t.id === e.target.value);
            if (selected) {
              setDraft(
                structuredClone(
                  studioDrafts.get(`${ownerId}:${selected.id}`) ?? selected,
                ),
              );
              setStageIndex(0);
              setValidation(undefined);
              setNotice("");
            }
          }}
        >
          {templates.map((t) => (
            <option key={t.id} value={t.id}>
              {t.title} · v{t.version}
            </option>
          ))}
          {!templates.some((t) => t.id === draft.id) && (
            <option value={draft.id}>{draft.title} · 导入草稿</option>
          )}
        </select>
      </label>
      <div className="tea-studio-toolbar">
        <div className="tea-segments">
          {(
            [
              ["workflow", "业务流程"],
              ["agent", "AI 工作步骤"],
              ["debug", "模拟调试"],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              aria-pressed={tab === id}
              className={tab === id ? "active" : ""}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </div>
        <ActionButton
          variant="outline"
          size="sm"
          busy={busy === "validate"}
          disabled={!!busy}
          onClick={() => run("validate")}
        >
          <CheckCircle2 size={14} />
          校验配置
        </ActionButton>
      </div>
      {validation && (
        <div
          className={validation.valid ? "tea-success" : "tea-error"}
          role="status"
        >
          {validation.valid ? (
            `校验通过 · ${validation.preview.stages} 个主体节点，${validation.preview.tasks} 项任务`
          ) : (
            <div>
              {validation.errors.map((item, i) => (
                <p key={i}>
                  {item.path}：{item.message}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
      {tab === "workflow" && (
        <>
          <div className="tea-config-intro">
            <label className="tea-field">
              <span>模板名称 · v{draft.version}</span>
              <Input
                value={draft.title}
                onChange={(e) => update({ ...draft, title: e.target.value })}
              />
            </label>
            <label className="tea-field">
              <span>流程说明</span>
              <Input
                value={draft.description}
                onChange={(e) =>
                  update({ ...draft, description: e.target.value })
                }
              />
            </label>
            <div className="tea-form-row">
              <label className="tea-field">
                <span>出口国家</span>
                <Input
                  value={draft.origin}
                  onChange={(e) => update({ ...draft, origin: e.target.value })}
                />
              </label>
              <label className="tea-field">
                <span>目的国家</span>
                <Input
                  value={draft.destination}
                  onChange={(e) =>
                    update({ ...draft, destination: e.target.value })
                  }
                />
              </label>
            </div>
            <details className="tea-config-details">
              <summary>模式设置 · {draft.modes.length} 种</summary>
              {draft.modes.map((mode, i) => (
                <div className="tea-mode-config" key={mode.id}>
                  <label className="tea-field">
                    <span>模式名称</span>
                    <Input
                      value={mode.name}
                      onChange={(e) =>
                        update({
                          ...draft,
                          modes: draft.modes.map((m, j) =>
                            j === i ? { ...m, name: e.target.value } : m,
                          ),
                        })
                      }
                    />
                  </label>
                  <label className="tea-field">
                    <span>业务说明</span>
                    <Input
                      value={mode.description}
                      onChange={(e) =>
                        update({
                          ...draft,
                          modes: draft.modes.map((m, j) =>
                            j === i ? { ...m, description: e.target.value } : m,
                          ),
                        })
                      }
                    />
                  </label>
                  <label className="tea-checkbox">
                    <input
                      type="checkbox"
                      checked={mode.enabled}
                      onChange={(e) =>
                        update({
                          ...draft,
                          modes: draft.modes.map((m, j) =>
                            j === i ? { ...m, enabled: e.target.checked } : m,
                          ),
                        })
                      }
                    />
                    启用
                  </label>
                </div>
              ))}
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  update({
                    ...draft,
                    modes: [
                      ...draft.modes,
                      {
                        id: makeId("mode"),
                        name: "新模式",
                        description: "",
                        enabled: true,
                      },
                    ],
                  })
                }
              >
                <Plus size={14} />
                新增模式
              </Button>
            </details>
          </div>
          <div className="tea-config-layout">
            <div className="tea-config-stages">
              <span className="tea-list-label">主体节点 · 按顺序推进</span>
              {draft.stages.map((stage, i) => (
                <button
                  className={stageIndex === i ? "active" : ""}
                  key={stage.id}
                  onClick={() => setStageIndex(i)}
                >
                  <span>{String(i + 1).padStart(2, "0")}</span>
                  <div>
                    <strong>{stage.name}</strong>
                    <small>
                      {stage.actor} · {stage.tasks.length} 项任务
                    </small>
                  </div>
                </button>
              ))}
              <Button
                variant="ghost"
                onClick={() => {
                  update({
                    ...draft,
                    stages: [
                      ...draft.stages,
                      {
                        id: makeId("stage"),
                        name: "新主体节点",
                        actor: "待配置主体",
                        description: "",
                        tasks: [],
                      },
                    ],
                  });
                  setStageIndex(draft.stages.length);
                }}
              >
                <Plus size={14} />
                添加节点
              </Button>
            </div>
            {selected && (
              <div className="tea-config-stage-editor">
                <div className="tea-editor-title">
                  <h3>节点 {stageIndex + 1}</h3>
                  <div>
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      aria-label="节点前移"
                      disabled={stageIndex === 0}
                      onClick={() => {
                        const stages = [...draft.stages];
                        [stages[stageIndex - 1], stages[stageIndex]] = [
                          stages[stageIndex]!,
                          stages[stageIndex - 1]!,
                        ];
                        update({ ...draft, stages });
                        setStageIndex(stageIndex - 1);
                      }}
                    >
                      <ArrowUp size={14} />
                    </Button>
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      aria-label="节点后移"
                      disabled={stageIndex === draft.stages.length - 1}
                      onClick={() => {
                        const stages = [...draft.stages];
                        [stages[stageIndex + 1], stages[stageIndex]] = [
                          stages[stageIndex]!,
                          stages[stageIndex + 1]!,
                        ];
                        update({ ...draft, stages });
                        setStageIndex(stageIndex + 1);
                      }}
                    >
                      <ArrowDown size={14} />
                    </Button>
                  </div>
                </div>
                <div className="tea-form-row">
                  <label className="tea-field">
                    <span>节点名称</span>
                    <Input
                      value={selected.name}
                      onChange={(e) =>
                        updateStage({ ...selected, name: e.target.value })
                      }
                    />
                  </label>
                  <label className="tea-field">
                    <span>面向主体</span>
                    <Input
                      value={selected.actor}
                      onChange={(e) =>
                        updateStage({ ...selected, actor: e.target.value })
                      }
                    />
                  </label>
                </div>
                <label className="tea-field">
                  <span>阶段目标</span>
                  <Textarea
                    value={selected.description}
                    onChange={(e) =>
                      updateStage({ ...selected, description: e.target.value })
                    }
                  />
                </label>
                <div className="tea-list-label">内部任务 · 可并行办理</div>
                {selected.tasks.map((task, i) => (
                  <details
                    className="tea-config-task"
                    key={task.id}
                    open={i === 0}
                  >
                    <summary>
                      <span>
                        {i + 1}. {task.title}
                      </span>
                      <span>{task.required ? "必办" : "选办"}</span>
                    </summary>
                    <div>
                      <label className="tea-field">
                        <span>任务名称</span>
                        <Input
                          value={task.title}
                          onChange={(e) =>
                            updateTask(i, { ...task, title: e.target.value })
                          }
                        />
                      </label>
                      <label className="tea-field">
                        <span>办理说明</span>
                        <Textarea
                          value={task.description}
                          onChange={(e) =>
                            updateTask(i, {
                              ...task,
                              description: e.target.value,
                            })
                          }
                        />
                      </label>
                      <div className="tea-rule-toggles">
                        {(
                          [
                            ["required", "必办任务"],
                            ["require_document", "需要关联材料"],
                            ["require_confirmation", "需要用户确认"],
                          ] as const
                        ).map(([key, label]) => (
                          <label className="tea-checkbox" key={key}>
                            <input
                              type="checkbox"
                              checked={task[key]}
                              onChange={(e) =>
                                updateTask(i, {
                                  ...task,
                                  [key]: e.target.checked,
                                })
                              }
                            />
                            {label}
                          </label>
                        ))}
                      </div>
                      <div className="tea-list-label">采集字段</div>
                      {task.fields.map((field, fi) => (
                        <div className="tea-field-config" key={fi}>
                          <label className="tea-field">
                            <span>字段名称 · {`{{${field.key}}}`}</span>
                            <Input
                              value={field.label}
                              onChange={(e) =>
                                updateTask(i, {
                                  ...task,
                                  fields: task.fields.map((f, j) =>
                                    j === fi
                                      ? { ...f, label: e.target.value }
                                      : f,
                                  ),
                                })
                              }
                            />
                          </label>
                          <label className="tea-field">
                            <span>输入类型</span>
                            <select
                              value={field.type}
                              onChange={(e) =>
                                updateTask(i, {
                                  ...task,
                                  fields: task.fields.map((f, j) =>
                                    j === fi
                                      ? {
                                          ...f,
                                          type: e.target.value as
                                            | "text"
                                            | "number"
                                            | "textarea",
                                        }
                                      : f,
                                  ),
                                })
                              }
                            >
                              <option value="text">文字</option>
                              <option value="number">数字</option>
                              <option value="textarea">多行文字</option>
                            </select>
                          </label>
                          <label className="tea-checkbox">
                            <input
                              type="checkbox"
                              checked={field.required}
                              onChange={(e) =>
                                updateTask(i, {
                                  ...task,
                                  fields: task.fields.map((f, j) =>
                                    j === fi
                                      ? { ...f, required: e.target.checked }
                                      : f,
                                  ),
                                })
                              }
                            />
                            必填
                          </label>
                        </div>
                      ))}
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          updateTask(i, {
                            ...task,
                            fields: [
                              ...task.fields,
                              {
                                key: makeId("field"),
                                label: "新字段",
                                type: "text",
                                required: true,
                              },
                            ],
                          })
                        }
                      >
                        <Plus size={14} />
                        添加字段
                      </Button>
                      <label className="tea-field">
                        <span>文稿模板 · 可使用字段占位符</span>
                        <Textarea
                          value={task.output_template ?? ""}
                          onChange={(e) =>
                            updateTask(i, {
                              ...task,
                              output_template: e.target.value,
                            })
                          }
                          placeholder="例如：产品名称：{{product_name}}"
                        />
                      </label>
                    </div>
                  </details>
                ))}
                <Button
                  variant="outline"
                  onClick={() =>
                    updateStage({
                      ...selected,
                      tasks: [
                        ...selected.tasks,
                        {
                          id: makeId("task"),
                          title: "新办理任务",
                          description: "",
                          required: true,
                          fields: [],
                          require_document: false,
                          require_confirmation: true,
                        },
                      ],
                    })
                  }
                >
                  <Plus size={15} />
                  添加任务
                </Button>
              </div>
            )}
          </div>
        </>
      )}
      {tab === "agent" && (
        <div className="tea-agent-config">
          <div className="tea-form-row">
            <label className="tea-field">
              <span>输出语言</span>
              <Input
                value={draft.agent.language}
                onChange={(e) =>
                  update({
                    ...draft,
                    agent: { ...draft.agent, language: e.target.value },
                  })
                }
              />
            </label>
            <label className="tea-field">
              <span>最多检索条数</span>
              <Input
                type="number"
                min={1}
                max={20}
                value={draft.agent.retrieval_limit}
                onChange={(e) =>
                  update({
                    ...draft,
                    agent: {
                      ...draft.agent,
                      retrieval_limit: Number(e.target.value),
                    },
                  })
                }
              />
            </label>
          </div>
          <label className="tea-field">
            <span>指导要求</span>
            <Textarea
              rows={5}
              value={draft.agent.instructions}
              onChange={(e) =>
                update({
                  ...draft,
                  agent: { ...draft.agent, instructions: e.target.value },
                })
              }
            />
          </label>
          <div className="tea-agent-steps">
            {draft.agent.steps.map((step, i) => (
              <div key={step.id}>
                <span className="tea-step-number">{i + 1}</span>
                <Settings2 size={17} />
                <label className="tea-field">
                  <span>{step.type}</span>
                  <Input
                    value={step.label}
                    onChange={(e) =>
                      update({
                        ...draft,
                        agent: {
                          ...draft.agent,
                          steps: draft.agent.steps.map((s, j) =>
                            j === i ? { ...s, label: e.target.value } : s,
                          ),
                        },
                      })
                    }
                  />
                </label>
                <label className="tea-checkbox">
                  <input
                    type="checkbox"
                    checked={step.enabled}
                    onChange={(e) =>
                      update({
                        ...draft,
                        agent: {
                          ...draft.agent,
                          steps: draft.agent.steps.map((s, j) =>
                            j === i ? { ...s, enabled: e.target.checked } : s,
                          ),
                        },
                      })
                    }
                  />
                  启用
                </label>
              </div>
            ))}
          </div>
          <div className="tea-note">
            调试会运行当前配置的步骤，展示输入依据和检查结果。对真实业务的核验仍使用该业务绑定的版本。
          </div>
        </div>
      )}
      {tab === "debug" && (
        <div className="tea-debug-layout">
          <div>
            <div className="tea-panel-label">
              <FlaskConical size={18} />
              模拟用户输入
            </div>
            <Textarea
              rows={8}
              value={debugInput}
              onChange={(e) => setDebugInput(e.target.value)}
            />
            <ActionButton
              busy={busy === "debug"}
              disabled={!!busy || !debugInput.trim()}
              onClick={() => run("debug")}
            >
              运行调试
            </ActionButton>
            <p className="tea-small-note">
              本次调试不会修改任何真实业务或任务进度。
            </p>
          </div>
          <div className="tea-debug-output">
            {debug ? (
              <>
                <h3>运行结果</h3>
                <p className="tea-prose">{debug.output}</p>
                <Trace steps={debug.trace} />
                <Checks checks={debug.checks} />
              </>
            ) : (
              <div className="tea-empty">
                <FlaskConical size={32} />
                <h3>先验证，再交给商家</h3>
                <p>试试“资料完整”“信息缺失”或“内容冲突”的输入。</p>
              </div>
            )}
          </div>
        </div>
      )}
    </fieldset>
  );
}
