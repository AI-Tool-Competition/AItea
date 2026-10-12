"use client";

import { Copy, Plus, Settings2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { makeConfigId, moveItem, stepTypes } from "@/core/tea/configuration";
import {
  type AgentConfig,
  type AgentStep,
  type Template,
  type TraceStep,
} from "@/core/tea/types";

import { ConfirmAction, EditorInspector, OrderActions } from "./admin-shared";
import { ConfigurationCanvas } from "./configuration-canvas";

export function AnalysisEditor({
  draft,
  onChange,
  stageId,
  onStage,
  stepId,
  onStep,
  trace,
  editingDefault,
  onEditingDefault,
  debugContent,
  debugOpen = false,
  onDebugClose,
  editRequest = 0,
}: {
  draft: Template;
  onChange: (draft: Template) => void;
  stageId: string;
  onStage: (id: string) => void;
  stepId: string;
  onStep: (id: string) => void;
  trace: TraceStep[];
  editingDefault: boolean;
  onEditingDefault: (value: boolean) => void;
  debugContent?: ReactNode;
  debugOpen?: boolean;
  onDebugClose?: () => void;
  editRequest?: number;
}) {
  const [newType, setNewType] = useState<AgentStep["type"]>("guide");
  const [tab, setTab] = useState<"step" | "settings">("step");
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    if (editRequest) {
      setEditing(true);
      setTab("step");
    }
  }, [editRequest]);
  const stage = draft.stages.find((s) => s.id === stageId) ?? draft.stages[0]!;
  const agent = editingDefault ? draft.agent : (stage.analysis ?? draft.agent);
  const inherited = !editingDefault && !stage.analysis;
  const selected =
    agent.steps.find((step) => step.id === stepId) ?? agent.steps[0];
  const index = selected ? agent.steps.indexOf(selected) : 0;
  function changeAgent(next: AgentConfig) {
    if (editingDefault) onChange({ ...draft, agent: next });
    else
      onChange({
        ...draft,
        stages: draft.stages.map((s) =>
          s.id === stage.id ? { ...s, analysis: next } : s,
        ),
      });
  }
  function changeStep(next: AgentStep) {
    changeAgent({
      ...agent,
      steps: agent.steps.map((step) => (step.id === next.id ? next : step)),
    });
  }
  function selectStep(id: string) {
    onStep(id);
    setTab("step");
    if (!debugOpen) setEditing(true);
  }
  return (
    <div className="ta-editor-grid ta-analysis-grid">
      <div className="ta-editor-main">
        <div className="ta-analysis-toolbar">
          <label>
            <span>进入节点</span>
            <select
              value={stage.id}
              onChange={(e) => {
                onStage(e.target.value);
                onEditingDefault(false);
              }}
              aria-label="分析对应节点"
            >
              {draft.stages.map((item, i) => (
                <option key={item.id} value={item.id}>
                  {i + 1}. {item.name}
                </option>
              ))}
            </select>
          </label>
          <span className="ta-badge">
            {editingDefault
              ? "正在编辑默认流程"
              : inherited
                ? "沿用默认"
                : "节点专用"}
          </span>
          <Button
            variant="ghost"
            size="sm"
            data-inspector-trigger
            onClick={() => {
              onDebugClose?.();
              setTab("settings");
              setEditing(true);
            }}
          >
            <Settings2 size={15} />
            流程设置
          </Button>
        </div>
        <ConfigurationCanvas
          label="主动分析工作流"
          selected={selected?.id ?? ""}
          onSelect={selectStep}
          trace={trace}
          items={agent.steps.map((step) => ({
            id: step.id,
            label: step.label,
            subtitle: stepTypes[step.type].label,
            kind: step.type,
            enabled: step.enabled,
          }))}
        />
        <div className="ta-canvas-footer">
          <select
            className="ta-step-picker"
            data-inspector-trigger
            aria-label="选择分析步骤"
            value={selected?.id ?? ""}
            onChange={(e) => selectStep(e.target.value)}
          >
            {agent.steps.map((step, i) => (
              <option key={step.id} value={step.id}>
                {i + 1}. {step.label}
              </option>
            ))}
          </select>
          <div className="ta-add-step">
            <select
              aria-label="新增步骤类型"
              value={newType}
              onChange={(e) => setNewType(e.target.value as AgentStep["type"])}
            >
              {Object.entries(stepTypes).map(([id, value]) => (
                <option value={id} key={id}>
                  {value.label}
                </option>
              ))}
            </select>
            <Button
              variant="outline"
              size="sm"
              data-inspector-trigger
              disabled={inherited || agent.steps.length >= 20}
              onClick={() => {
                const step = {
                  id: makeConfigId("step"),
                  type: newType,
                  label: stepTypes[newType].label,
                  enabled: true,
                  instructions: "",
                };
                changeAgent({ ...agent, steps: [...agent.steps, step] });
                selectStep(step.id);
              }}
            >
              <Plus size={14} />
              添加步骤
            </Button>
          </div>
        </div>
      </div>
      <EditorInspector
        open={editing || debugOpen}
        onClose={() => {
          setEditing(false);
          onDebugClose?.();
        }}
        label="分析编辑栏"
      >
        {debugOpen ? (
          debugContent
        ) : (
          <>
            <div
              className="ta-inspector-tabs"
              role="tablist"
              aria-label="分析编辑内容"
            >
              <button
                role="tab"
                aria-selected={tab === "step"}
                onClick={() => setTab("step")}
              >
                步骤设置
              </button>
              <button
                role="tab"
                aria-selected={tab === "settings"}
                onClick={() => setTab("settings")}
              >
                流程设置
              </button>
            </div>
            <div className="ta-inspector-body">
              {inherited && (
                <div className="ta-inheritance">
                  <p>沿用默认分析</p>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => changeAgent(structuredClone(agent))}
                  >
                    <Copy size={13} />
                    单独配置
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      onEditingDefault(true);
                      onStep("");
                    }}
                  >
                    编辑默认
                  </Button>
                </div>
              )}
              {editingDefault && (
                <div className="ta-inheritance">
                  <p>修改影响所有沿用默认的节点。</p>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      onEditingDefault(false);
                      onStep("");
                    }}
                  >
                    返回节点配置
                  </Button>
                </div>
              )}
              {tab === "settings" ? (
                <>
                  <div className="ta-inspector-header">
                    <h3>{editingDefault ? "默认分析流程" : "分析流程设置"}</h3>
                  </div>
                  <fieldset disabled={inherited}>
                    <label className="tea-field">
                      <span>希望助手怎样分析</span>
                      <Textarea
                        rows={7}
                        maxLength={10000}
                        value={agent.instructions}
                        onChange={(e) =>
                          changeAgent({
                            ...agent,
                            instructions: e.target.value,
                          })
                        }
                        placeholder="先说明缺项，再给出建议；每条建议标注依据。"
                      />
                    </label>
                    <label className="tea-field">
                      <span>输出语言</span>
                      <Input
                        maxLength={50}
                        value={agent.language}
                        onChange={(e) =>
                          changeAgent({ ...agent, language: e.target.value })
                        }
                      />
                    </label>
                    <label className="tea-field">
                      <span>最多引用几段资料</span>
                      <Input
                        type="number"
                        min={1}
                        max={20}
                        value={agent.retrieval_limit}
                        onChange={(e) =>
                          changeAgent({
                            ...agent,
                            retrieval_limit: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                  </fieldset>
                  {!editingDefault && !inherited && (
                    <ConfirmAction
                      title="恢复默认分析流程？"
                      description="此节点的专用步骤会从草稿移除。其他节点不受影响。"
                      onConfirm={() =>
                        onChange({
                          ...draft,
                          stages: draft.stages.map((s) =>
                            s.id === stage.id ? { ...s, analysis: null } : s,
                          ),
                        })
                      }
                    >
                      恢复默认流程
                    </ConfirmAction>
                  )}
                  {!editingDefault && !inherited && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        onEditingDefault(true);
                        onStep("");
                      }}
                    >
                      编辑默认流程
                    </Button>
                  )}
                </>
              ) : selected ? (
                <>
                  <div className="ta-inspector-header">
                    <div>
                      <small>步骤 {index + 1}</small>
                      <h3>{stepTypes[selected.type].label}</h3>
                    </div>
                    <fieldset disabled={inherited}>
                      <OrderActions
                        label="步骤"
                        index={index}
                        count={agent.steps.length}
                        onMove={(direction) =>
                          changeAgent({
                            ...agent,
                            steps: moveItem(
                              agent.steps,
                              index,
                              index + direction,
                            ),
                          })
                        }
                        onRemove={
                          agent.steps.length > 1
                            ? () =>
                                changeAgent({
                                  ...agent,
                                  steps: agent.steps.filter(
                                    (step) => step.id !== selected.id,
                                  ),
                                })
                            : undefined
                        }
                      />
                    </fieldset>
                  </div>
                  <fieldset disabled={inherited}>
                    <label className="tea-field">
                      <span>步骤名称</span>
                      <Input
                        value={selected.label}
                        maxLength={100}
                        onChange={(e) =>
                          changeStep({ ...selected, label: e.target.value })
                        }
                      />
                    </label>
                    <label className="tea-field">
                      <span>这一步做什么</span>
                      <select
                        value={selected.type}
                        onChange={(e) =>
                          changeStep({
                            ...selected,
                            type: e.target.value as AgentStep["type"],
                          })
                        }
                      >
                        {Object.entries(stepTypes).map(([id, value]) => (
                          <option key={id} value={id}>
                            {value.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <p className="ta-step-explainer">
                      {stepTypes[selected.type].description}
                    </p>
                    {(selected.type === "retrieve" ||
                      selected.type === "guide") && (
                      <label className="tea-field">
                        <span>
                          {selected.type === "retrieve"
                            ? "检索关注点"
                            : "这一步的分析要求"}
                        </span>
                        <Textarea
                          rows={7}
                          maxLength={3000}
                          value={selected.instructions ?? ""}
                          onChange={(e) =>
                            changeStep({
                              ...selected,
                              instructions: e.target.value,
                            })
                          }
                          placeholder={
                            selected.type === "retrieve"
                              ? "例如：产品成分、净含量、企业名称相关原文"
                              : "例如：先说明缺项，再给出三条可执行的建议"
                          }
                        />
                      </label>
                    )}
                    <label className="tea-checkbox">
                      <input
                        type="checkbox"
                        checked={selected.enabled}
                        onChange={(e) =>
                          changeStep({ ...selected, enabled: e.target.checked })
                        }
                      />
                      启用这一步
                    </label>
                  </fieldset>
                </>
              ) : (
                <p className="ta-hint">添加第一个分析步骤。</p>
              )}
            </div>
          </>
        )}
      </EditorInspector>
    </div>
  );
}
