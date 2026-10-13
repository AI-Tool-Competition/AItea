"use client";

import {
  ChevronDown,
  ChevronRight,
  FileText,
  Plus,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { makeConfigId, moveItem } from "@/core/tea/configuration";
import {
  type FieldDefinition,
  type StageDefinition,
  type TaskDefinition,
  type Template,
} from "@/core/tea/types";
import { cn } from "@/lib/utils";

import { ConfirmAction, EditorInspector, OrderActions } from "./admin-shared";

type Selection =
  | { kind: "config" | "stage"; stageId: string }
  | { kind: "task"; stageId: string; taskId: string }
  | { kind: "field"; stageId: string; taskId: string; fieldIndex: number };

function newTask(): TaskDefinition {
  return {
    id: makeConfigId("task"),
    title: "新的办理任务",
    description: "",
    required: true,
    fields: [],
    require_document: false,
    require_confirmation: true,
  };
}

const fieldTypes: Record<FieldDefinition["type"], string> = {
  text: "短文字",
  textarea: "多行文字",
  number: "数字",
  select: "单选",
};

export function WorkflowEditor({
  draft,
  onChange,
  stageId,
  onStage,
  onAnalyze,
  debugContent,
  debugOpen = false,
  onDebugClose,
  editRequest = 0,
}: {
  draft: Template;
  onChange: (draft: Template) => void;
  stageId: string;
  onStage: (id: string) => void;
  onAnalyze: () => void;
  debugContent?: ReactNode;
  debugOpen?: boolean;
  onDebugClose?: () => void;
  editRequest?: number;
}) {
  const stageIndex = Math.max(
    0,
    draft.stages.findIndex((stage) => stage.id === stageId),
  );
  const stage = draft.stages[stageIndex];
  const [selection, setSelection] = useState<Selection>({
    kind: "stage",
    stageId: stage?.id ?? "",
  });
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    if (editRequest) setEditing(true);
  }, [editRequest]);
  const [expandedStages, setExpandedStages] = useState(
    () => new Set([stage?.id ?? ""]),
  );
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(
    () => new Set(),
  );

  // A stage selected by the parent must replace any inspector for the old stage.
  let selected: Selection =
    selection.stageId === stage?.id
      ? selection
      : { kind: stage ? "stage" : "config", stageId: stage?.id ?? "" };
  const selectedTaskId = "taskId" in selected ? selected.taskId : undefined;
  const taskIndex =
    stage?.tasks.findIndex((task) => task.id === selectedTaskId) ?? -1;
  const task = stage?.tasks[taskIndex];
  if ((selected.kind === "task" || selected.kind === "field") && !task)
    selected = { kind: "stage", stageId: stage?.id ?? "" };
  if (selected.kind === "field" && !task?.fields[selected.fieldIndex])
    selected = { kind: "task", stageId: stage?.id ?? "", taskId: task!.id };
  const fieldIndex = selected.kind === "field" ? selected.fieldIndex : -1;
  const field = task?.fields[fieldIndex];
  const fullyExpanded = draft.stages.every(
    (item) =>
      expandedStages.has(item.id) &&
      item.tasks.every((child) => expandedTasks.has(child.id)),
  );

  function select(next: Selection) {
    setSelection(next);
    setEditing(true);
    onDebugClose?.();
    if (next.stageId !== stageId) onStage(next.stageId);
  }
  function updateStage(next: StageDefinition) {
    onChange({
      ...draft,
      stages: draft.stages.map((item) => (item.id === next.id ? next : item)),
    });
  }
  function updateTask(next: TaskDefinition) {
    if (stage)
      updateStage({
        ...stage,
        tasks: stage.tasks.map((item) => (item.id === next.id ? next : item)),
      });
  }
  function updateField(next: FieldDefinition) {
    if (task)
      updateTask({
        ...task,
        fields: task.fields.map((item, index) =>
          index === fieldIndex ? next : item,
        ),
      });
  }
  function addTask(parent: StageDefinition) {
    const next = newTask();
    updateStage({ ...parent, tasks: [...parent.tasks, next] });
    setExpandedStages((previous) => new Set(previous).add(parent.id));
    select({ kind: "task", stageId: parent.id, taskId: next.id });
  }
  function addField(parent: StageDefinition, child: TaskDefinition) {
    const next = {
      ...child,
      fields: [
        ...child.fields,
        {
          key: makeConfigId("field"),
          label: "新填写项",
          type: "text" as const,
          required: true,
        },
      ],
    };
    updateStage({
      ...parent,
      tasks: parent.tasks.map((item) => (item.id === child.id ? next : item)),
    });
    setExpandedTasks((previous) => new Set(previous).add(child.id));
    select({
      kind: "field",
      stageId: parent.id,
      taskId: child.id,
      fieldIndex: child.fields.length,
    });
  }

  const heading = {
    config: "流程设置",
    stage: "节点设置",
    task: "任务设置",
    field: "填写项设置",
  }[selected.kind];
  return (
    <div className="ta-editor-grid ta-workflow-workspace">
      <section className="ta-workflow-tree" aria-label="流程结构">
        <div className="ta-tree-toolbar">
          <h3>流程结构</h3>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => {
              setExpandedStages(
                new Set(
                  fullyExpanded ? [] : draft.stages.map((item) => item.id),
                ),
              );
              setExpandedTasks(
                new Set(
                  fullyExpanded
                    ? []
                    : draft.stages.flatMap((item) =>
                        item.tasks.map((child) => child.id),
                      ),
                ),
              );
            }}
          >
            {fullyExpanded ? "全部收起" : "全部展开"}
          </Button>
        </div>
        <ul className="ta-tree-list">
          <li>
            <div
              className={cn(
                "ta-tree-row",
                selected.kind === "config" && "is-selected",
              )}
            >
              <span className="ta-tree-toggle" aria-hidden="true">
                <FileText size={16} />
              </span>
              <button
                type="button"
                className="ta-tree-label"
                data-inspector-trigger
                aria-label="流程基本信息"
                aria-pressed={selected.kind === "config"}
                onClick={() =>
                  select({ kind: "config", stageId: stage?.id ?? "" })
                }
              >
                <span>{draft.title || "未命名流程"}</span>
              </button>
              <span className="ta-tree-meta">基本信息与模式</span>
            </div>
          </li>
          {draft.stages.map((item, index) => {
            const opened = expandedStages.has(item.id);
            const name = item.name || "未命名节点";
            return (
              <li key={item.id}>
                <div
                  className={cn(
                    "ta-tree-row",
                    selected.kind === "stage" &&
                      stage?.id === item.id &&
                      "is-selected",
                  )}
                >
                  <button
                    type="button"
                    className="ta-tree-toggle"
                    aria-expanded={opened}
                    aria-label={`${opened ? "收起" : "展开"}节点：${name}`}
                    onClick={() =>
                      setExpandedStages((previous) => {
                        const next = new Set(previous);
                        if (opened) next.delete(item.id);
                        else next.add(item.id);
                        return next;
                      })
                    }
                  >
                    {opened ? (
                      <ChevronDown size={16} />
                    ) : (
                      <ChevronRight size={16} />
                    )}
                  </button>
                  <button
                    type="button"
                    className="ta-tree-label"
                    data-inspector-trigger
                    aria-label={name}
                    aria-pressed={
                      selected.kind === "stage" && stage?.id === item.id
                    }
                    onClick={() => select({ kind: "stage", stageId: item.id })}
                  >
                    <span>
                      {index + 1}. {name}
                    </span>
                    <small>{item.actor || "未填写办理主体"}</small>
                  </button>
                  <span className="ta-tree-meta">
                    {item.tasks.length} 项任务
                  </span>
                </div>
                {opened && (
                  <ul className="ta-tree-children">
                    {item.tasks.map((child) => {
                      const taskOpened = expandedTasks.has(child.id);
                      const title = child.title || "未命名任务";
                      return (
                        <li key={child.id}>
                          <div
                            className={cn(
                              "ta-tree-row",
                              selected.kind === "task" &&
                                task?.id === child.id &&
                                "is-selected",
                            )}
                          >
                            <button
                              type="button"
                              className="ta-tree-toggle"
                              aria-expanded={taskOpened}
                              aria-label={`${taskOpened ? "收起" : "展开"}任务：${title}`}
                              onClick={() =>
                                setExpandedTasks((previous) => {
                                  const next = new Set(previous);
                                  if (taskOpened) next.delete(child.id);
                                  else next.add(child.id);
                                  return next;
                                })
                              }
                            >
                              {taskOpened ? (
                                <ChevronDown size={15} />
                              ) : (
                                <ChevronRight size={15} />
                              )}
                            </button>
                            <button
                              type="button"
                              className="ta-tree-label"
                              data-inspector-trigger
                              aria-label={title}
                              aria-pressed={
                                selected.kind === "task" &&
                                task?.id === child.id
                              }
                              onClick={() =>
                                select({
                                  kind: "task",
                                  stageId: item.id,
                                  taskId: child.id,
                                })
                              }
                            >
                              <span>{title}</span>
                              <small>
                                {child.required ? "必办任务" : "按需办理"}
                              </small>
                            </button>
                            <span className="ta-tree-meta">
                              {child.fields.length} 个填写项
                            </span>
                          </div>
                          {taskOpened && (
                            <ul className="ta-tree-children">
                              {child.fields.map((entry, position) => {
                                const active =
                                  selected.kind === "field" &&
                                  task?.id === child.id &&
                                  fieldIndex === position;
                                const label = entry.label || "未命名填写项";
                                return (
                                  <li key={`${child.id}-${position}`}>
                                    <div
                                      className={cn(
                                        "ta-tree-row",
                                        active && "is-selected",
                                      )}
                                    >
                                      <span
                                        className="ta-tree-toggle"
                                        aria-hidden="true"
                                      >
                                        <FileText size={13} />
                                      </span>
                                      <button
                                        type="button"
                                        className="ta-tree-label"
                                        data-inspector-trigger
                                        aria-label={label}
                                        aria-pressed={active}
                                        onClick={() =>
                                          select({
                                            kind: "field",
                                            stageId: item.id,
                                            taskId: child.id,
                                            fieldIndex: position,
                                          })
                                        }
                                      >
                                        <span>{label}</span>
                                      </button>
                                      <span className="ta-tree-meta">
                                        {fieldTypes[entry.type]}
                                        {entry.required ? " · 必填" : ""}
                                      </span>
                                    </div>
                                  </li>
                                );
                              })}
                              <li>
                                <Button
                                  type="button"
                                  className="ta-tree-add"
                                  data-inspector-trigger
                                  variant="ghost"
                                  size="sm"
                                  disabled={child.fields.length >= 30}
                                  aria-label={`为${title}添加填写项`}
                                  onClick={() => addField(item, child)}
                                >
                                  <Plus size={13} />
                                  添加填写项
                                </Button>
                              </li>
                            </ul>
                          )}
                        </li>
                      );
                    })}
                    <li>
                      <Button
                        type="button"
                        className="ta-tree-add"
                        data-inspector-trigger
                        variant="ghost"
                        size="sm"
                        disabled={item.tasks.length >= 30}
                        aria-label={`为${name}添加任务`}
                        onClick={() => addTask(item)}
                      >
                        <Plus size={14} />
                        添加任务
                      </Button>
                    </li>
                  </ul>
                )}
              </li>
            );
          })}
          <li>
            <Button
              type="button"
              className="ta-tree-add"
              data-inspector-trigger
              variant="ghost"
              size="sm"
              disabled={draft.stages.length >= 20}
              onClick={() => {
                const next = {
                  id: makeConfigId("stage"),
                  name: "新的办理节点",
                  actor: "企业",
                  description: "",
                  tasks: [newTask()],
                };
                onChange({ ...draft, stages: [...draft.stages, next] });
                setExpandedStages((previous) => new Set(previous).add(next.id));
                select({ kind: "stage", stageId: next.id });
              }}
            >
              <Plus size={15} />
              添加办理节点
            </Button>
          </li>
        </ul>
      </section>
      <EditorInspector
        open={editing || debugOpen}
        onClose={() => {
          setEditing(false);
          onDebugClose?.();
        }}
        label="流程编辑栏"
      >
        {debugOpen ? (
          debugContent
        ) : (
          <>
            <div className="ta-inspector-header">
              <div>
                <small>
                  {selected.kind === "config"
                    ? "基本信息与办理模式"
                    : selected.kind === "stage"
                      ? `节点 ${stageIndex + 1}`
                      : `${stage?.name} / ${selected.kind === "task" ? "任务" : task?.title}`}
                </small>
                <h3>{heading}</h3>
              </div>
              {selected.kind === "stage" && stage && (
                <OrderActions
                  label="节点"
                  index={stageIndex}
                  count={draft.stages.length}
                  onMove={(direction) =>
                    onChange({
                      ...draft,
                      stages: moveItem(
                        draft.stages,
                        stageIndex,
                        stageIndex + direction,
                      ),
                    })
                  }
                  onRemove={
                    draft.stages.length > 1
                      ? () => {
                          const next = draft.stages.filter(
                            (item) => item.id !== stage.id,
                          );
                          onChange({ ...draft, stages: next });
                          select({
                            kind: "stage",
                            stageId: next[Math.max(0, stageIndex - 1)]!.id,
                          });
                        }
                      : undefined
                  }
                />
              )}
              {selected.kind === "task" && task && stage && (
                <OrderActions
                  label="任务"
                  index={taskIndex}
                  count={stage.tasks.length}
                  onMove={(direction) =>
                    updateStage({
                      ...stage,
                      tasks: moveItem(
                        stage.tasks,
                        taskIndex,
                        taskIndex + direction,
                      ),
                    })
                  }
                  onRemove={
                    stage.tasks.length > 1
                      ? () => {
                          updateStage({
                            ...stage,
                            tasks: stage.tasks.filter(
                              (item) => item.id !== task.id,
                            ),
                          });
                          select({ kind: "stage", stageId: stage.id });
                        }
                      : undefined
                  }
                />
              )}
              {selected.kind === "field" && field && task && stage && (
                <OrderActions
                  label="填写项"
                  index={fieldIndex}
                  count={task.fields.length}
                  onMove={(direction) => {
                    updateTask({
                      ...task,
                      fields: moveItem(
                        task.fields,
                        fieldIndex,
                        fieldIndex + direction,
                      ),
                    });
                    select({
                      kind: "field",
                      stageId: stage.id,
                      taskId: task.id,
                      fieldIndex: fieldIndex + direction,
                    });
                  }}
                  onRemove={() => {
                    updateTask({
                      ...task,
                      fields: task.fields.filter(
                        (_, index) => index !== fieldIndex,
                      ),
                    });
                    select({
                      kind: "task",
                      stageId: stage.id,
                      taskId: task.id,
                    });
                  }}
                />
              )}
            </div>
            <div className="ta-inspector-body">
              {selected.kind === "config" && (
                <ConfigurationFields draft={draft} onChange={onChange} />
              )}
              {selected.kind === "stage" && stage && (
                <>
                  <label className="tea-field">
                    <span>节点名称</span>
                    <Input
                      value={stage.name}
                      maxLength={100}
                      onChange={(event) =>
                        updateStage({ ...stage, name: event.target.value })
                      }
                      placeholder="例如：产品资料准备"
                    />
                  </label>
                  <label className="tea-field">
                    <span>办理主体</span>
                    <Input
                      value={stage.actor}
                      maxLength={100}
                      onChange={(event) =>
                        updateStage({ ...stage, actor: event.target.value })
                      }
                      placeholder="谁来完成这个节点"
                    />
                  </label>
                  <label className="tea-field">
                    <span>办理目标</span>
                    <Textarea
                      value={stage.description}
                      maxLength={2000}
                      onChange={(event) =>
                        updateStage({
                          ...stage,
                          description: event.target.value,
                        })
                      }
                      placeholder="这个阶段要完成什么？"
                    />
                  </label>
                  <Button type="button" variant="outline" onClick={onAnalyze}>
                    <Sparkles size={15} />
                    配置节点分析
                  </Button>
                </>
              )}
              {selected.kind === "task" && task && (
                <TaskFields task={task} onChange={updateTask} />
              )}
              {selected.kind === "field" && field && (
                <FieldFields field={field} onChange={updateField} />
              )}
            </div>
          </>
        )}
      </EditorInspector>
    </div>
  );
}

function ConfigurationFields({
  draft,
  onChange,
}: {
  draft: Template;
  onChange: (draft: Template) => void;
}) {
  return (
    <>
      <label className="tea-field">
        <span>流程名称</span>
        <Input
          maxLength={200}
          value={draft.title}
          onChange={(event) =>
            onChange({ ...draft, title: event.target.value })
          }
        />
      </label>
      <label className="tea-field">
        <span>流程说明</span>
        <Textarea
          maxLength={3000}
          value={draft.description}
          onChange={(event) =>
            onChange({ ...draft, description: event.target.value })
          }
        />
      </label>
      <div className="tea-form-row">
        <label className="tea-field">
          <span>出发国家</span>
          <Input
            maxLength={100}
            value={draft.origin}
            onChange={(event) =>
              onChange({ ...draft, origin: event.target.value })
            }
          />
        </label>
        <label className="tea-field">
          <span>目的国家</span>
          <Input
            maxLength={100}
            value={draft.destination}
            onChange={(event) =>
              onChange({ ...draft, destination: event.target.value })
            }
          />
        </label>
      </div>
      <div className="ta-subheading">
        <h4>办理模式</h4>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={draft.modes.length >= 20}
          onClick={() =>
            onChange({
              ...draft,
              modes: [
                ...draft.modes,
                {
                  id: makeConfigId("mode"),
                  name: "新模式",
                  description: "",
                  enabled: true,
                },
              ],
            })
          }
        >
          <Plus size={14} />
          添加模式
        </Button>
      </div>
      {draft.modes.map((mode) => (
        <div className="ta-mode-row" key={mode.id}>
          <label className="tea-field">
            <span>模式名称</span>
            <Input
              maxLength={100}
              value={mode.name}
              onChange={(event) =>
                onChange({
                  ...draft,
                  modes: draft.modes.map((item) =>
                    item.id === mode.id
                      ? { ...item, name: event.target.value }
                      : item,
                  ),
                })
              }
            />
          </label>
          <label className="tea-field">
            <span>适用情况</span>
            <Input
              maxLength={2000}
              value={mode.description}
              onChange={(event) =>
                onChange({
                  ...draft,
                  modes: draft.modes.map((item) =>
                    item.id === mode.id
                      ? { ...item, description: event.target.value }
                      : item,
                  ),
                })
              }
            />
          </label>
          <label className="tea-checkbox">
            <input
              type="checkbox"
              checked={mode.enabled}
              onChange={(event) =>
                onChange({
                  ...draft,
                  modes: draft.modes.map((item) =>
                    item.id === mode.id
                      ? { ...item, enabled: event.target.checked }
                      : item,
                  ),
                })
              }
            />
            启用
          </label>
          <ConfirmAction
            title="删除办理模式？"
            description="该模式将从当前草稿移除，已有业务保留自己的配置。"
            disabled={draft.modes.length <= 1}
            onConfirm={() =>
              onChange({
                ...draft,
                modes: draft.modes.filter((item) => item.id !== mode.id),
              })
            }
          >
            <Trash2 size={14} />
            <span className="sr-only">删除{mode.name}</span>
          </ConfirmAction>
        </div>
      ))}
    </>
  );
}

function TaskFields({
  task,
  onChange,
}: {
  task: TaskDefinition;
  onChange: (task: TaskDefinition) => void;
}) {
  return (
    <>
      <label className="tea-field">
        <span>任务名称</span>
        <Input
          maxLength={100}
          value={task.title}
          onChange={(event) => onChange({ ...task, title: event.target.value })}
        />
      </label>
      <label className="tea-field">
        <span>办理说明</span>
        <Textarea
          maxLength={2000}
          value={task.description}
          onChange={(event) =>
            onChange({ ...task, description: event.target.value })
          }
          placeholder="说明要准备哪些资料、怎样完成"
        />
      </label>
      <div className="ta-check-options">
        <label className="tea-checkbox">
          <input
            type="checkbox"
            checked={task.required}
            onChange={(event) =>
              onChange({ ...task, required: event.target.checked })
            }
          />
          必须完成才能推进
        </label>
        <label className="tea-checkbox">
          <input
            type="checkbox"
            checked={task.require_document}
            onChange={(event) =>
              onChange({ ...task, require_document: event.target.checked })
            }
          />
          需要关联材料
        </label>
        <label className="tea-checkbox">
          <input
            type="checkbox"
            checked={task.require_confirmation}
            onChange={(event) =>
              onChange({ ...task, require_confirmation: event.target.checked })
            }
          />
          需要办理人确认
        </label>
      </div>
      <details className="ta-advanced">
        <summary>文稿模板（可选）</summary>
        <Textarea
          aria-label="文稿模板"
          value={task.output_template ?? ""}
          maxLength={20000}
          onChange={(event) =>
            onChange({
              ...task,
              output_template: event.target.value || undefined,
            })
          }
        />
        <p>填写正文模板，未配置时使用现有默认文稿。</p>
      </details>
    </>
  );
}

function FieldFields({
  field,
  onChange,
}: {
  field: FieldDefinition;
  onChange: (field: FieldDefinition) => void;
}) {
  return (
    <>
      <label className="tea-field">
        <span>显示名称</span>
        <Input
          maxLength={100}
          value={field.label}
          onChange={(event) =>
            onChange({ ...field, label: event.target.value })
          }
        />
      </label>
      <label className="tea-field">
        <span>填写方式</span>
        <select
          value={field.type}
          onChange={(event) =>
            onChange({
              ...field,
              type: event.target.value as FieldDefinition["type"],
              options:
                event.target.value === "select"
                  ? ["待核验", "已核验完成"]
                  : undefined,
              accepted_values: undefined,
            })
          }
        >
          <option value="text">短文字</option>
          <option value="textarea">多行文字</option>
          <option value="number">数字</option>
          <option value="select">单选</option>
        </select>
      </label>
      <label className="tea-checkbox">
        <input
          type="checkbox"
          checked={field.required}
          onChange={(event) =>
            onChange({
              ...field,
              required: event.target.checked,
              accepted_values: event.target.checked
                ? field.accepted_values
                : undefined,
            })
          }
        />
        必填
      </label>
      {field.type === "select" && (
        <>
          <label className="tea-field">
            <span>可选值（每行一项）</span>
            <Textarea
              value={field.options?.join("\n") ?? ""}
              onChange={(event) =>
                onChange({ ...field, options: event.target.value.split("\n") })
              }
            />
          </label>
          {field.required && (
            <label className="tea-field">
              <span>核验通过值（每行一项，留空表示任一有效选项）</span>
              <Textarea
                value={field.accepted_values?.join("\n") ?? ""}
                onChange={(event) =>
                  onChange({
                    ...field,
                    accepted_values:
                      event.target.value === ""
                        ? undefined
                        : event.target.value.split("\n"),
                  })
                }
              />
            </label>
          )}
        </>
      )}
      {field.type === "number" && (
        <label className="tea-field">
          <span>允许的最小值（可不填）</span>
          <Input
            type="number"
            value={field.min ?? ""}
            onChange={(event) =>
              onChange({
                ...field,
                min:
                  event.target.value === ""
                    ? undefined
                    : Number(event.target.value),
              })
            }
          />
        </label>
      )}
      <label className="tea-field">
        <span>填写提示</span>
        <Input
          value={field.placeholder ?? ""}
          onChange={(event) =>
            onChange({ ...field, placeholder: event.target.value })
          }
        />
      </label>
      <details className="ta-advanced">
        <summary>字段编号（用于文稿引用）</summary>
        <Input
          aria-label="字段编号"
          value={field.key}
          onChange={(event) => onChange({ ...field, key: event.target.value })}
        />
        <p>以小写字母开头，可包含数字与下划线。</p>
      </details>
    </>
  );
}
