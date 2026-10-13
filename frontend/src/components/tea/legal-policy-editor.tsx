"use client";

import { LockKeyhole, Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  errorMessage,
  getLegalPolicy,
  previewLegalAnalysis,
} from "@/core/tea/api";
import { makeConfigId } from "@/core/tea/configuration";
import {
  type LegalPolicy,
  type LegalPolicyDefaults,
  type LegalResult,
  type LegalRule,
  type Template,
} from "@/core/tea/types";

import { LegalResultView } from "./legal-result";
import { ActionButton, Busy, ErrorNotice } from "./shared";

const list = (value: string) => [
  ...new Set(
    value
      .split(/[,，\n]/)
      .map((item) => item.trim())
      .filter(Boolean),
  ),
];

function ListInput({
  value,
  onChange,
  placeholder,
  uppercase = false,
}: {
  uppercase?: boolean;
  value: string[];
  onChange: (value: string[]) => void;
  placeholder: string;
}) {
  const signature = JSON.stringify(value);
  const emitted = useRef(signature);
  const [text, setText] = useState(value.join(", "));
  useEffect(() => {
    if (emitted.current !== signature) {
      emitted.current = signature;
      setText(value.join(", "));
    }
  }, [signature, value]);
  return (
    <Input
      value={text}
      placeholder={placeholder}
      onChange={(e) => {
        setText(e.target.value);
        const next = list(e.target.value).map((item) =>
          uppercase ? item.toUpperCase() : item,
        );
        emitted.current = JSON.stringify(next);
        onChange(next);
      }}
    />
  );
}

export function LegalPolicyEditor({
  value,
  onChange,
  disabled,
  template,
  stageId,
  moduleId,
}: {
  value?: LegalPolicy | null;
  onChange: (policy: LegalPolicy) => void;
  disabled: boolean;
  template: Template;
  stageId: string;
  moduleId?: string;
}) {
  const [defaults, setDefaults] = useState<LegalPolicyDefaults>();
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [selectedId, setSelectedId] = useState("");
  const [question, setQuestion] = useState("");
  const [preview, setPreview] = useState<LegalResult>();
  const [busy, setBusy] = useState(false);
  const previewAbort = useRef<AbortController | null>(null);
  const latestTemplate = useRef(template);
  latestTemplate.current = template;
  useEffect(() => {
    const abort = new AbortController();
    setError("");
    void getLegalPolicy(abort.signal)
      .then((data) => {
        if (!abort.signal.aborted) setDefaults(data);
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      });
    return () => {
      abort.abort();
      previewAbort.current?.abort();
    };
  }, [attempt]);
  useEffect(() => {
    previewAbort.current?.abort();
    setBusy(false);
    setPreview(undefined);
  }, [stageId, template.id]);
  const policy = value ?? defaults?.policy;
  useEffect(() => {
    if (moduleId)
      setSelectedId(
        (value ?? defaults?.policy)?.rules.find(
          (item) => item.module === moduleId,
        )?.id ?? "",
      );
  }, [moduleId]); // eslint-disable-line react-hooks/exhaustive-deps
  const rule =
    policy?.rules.find((item) => item.id === selectedId) ?? policy?.rules[0];
  function editRule(patch: Partial<LegalRule>) {
    if (policy && rule)
      onChange({
        ...policy,
        rules: policy.rules.map((item) =>
          item.id === rule.id ? { ...item, ...patch } : item,
        ),
      });
  }
  async function runPreview() {
    if (busy || !question.trim()) return;
    const abort = new AbortController();
    previewAbort.current = abort;
    const submitted = template;
    setBusy(true);
    setError("");
    setPreview(undefined);
    try {
      const result = await previewLegalAnalysis(
        submitted,
        stageId,
        question.trim(),
        abort.signal,
      );
      if (!abort.signal.aborted && submitted === latestTemplate.current)
        setPreview(result);
      else if (!abort.signal.aborted)
        setError("配置已改变，请使用当前草稿重新试运行。");
    } catch (e) {
      if (!abort.signal.aborted) setError(errorMessage(e));
    } finally {
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  return (
    <div className="tea-legal-policy">
      <div className="ta-inspector-header">
        <h3>法律分析规则</h3>
      </div>
      <p className="ta-hint">
        规则随当前模板草稿保存和发布。已创建业务保留原配置。
      </p>
      {error && (
        <ErrorNotice message={error} onRetry={() => setAttempt((n) => n + 1)} />
      )}
      {!policy ? (
        !error && <Busy label="读取法律规则…" />
      ) : (
        <>
          <fieldset disabled={disabled}>
            <label className="tea-checkbox">
              <input
                type="checkbox"
                checked={policy.enabled}
                onChange={(e) =>
                  onChange({ ...policy, enabled: e.target.checked })
                }
              />
              在对话中启用法律分析
            </label>
            <label className="tea-field">
              <span>额外适用国家</span>
              <ListInput
                value={policy.jurisdictions}
                uppercase
                placeholder="国家代码，以逗号分隔，例如 SG, MY"
                onChange={(jurisdictions) =>
                  onChange({ ...policy, jurisdictions })
                }
              />
            </label>
            <p className="ta-hint">
              出发国、目的国自动纳入。监管地域、法院管辖和准据法分别分析。
            </p>
            <div className="tea-legal-number-fields">
              <label className="tea-field">
                <span>每组追问上限</span>
                <Input
                  type="number"
                  min={1}
                  max={5}
                  value={policy.question_limit}
                  onChange={(e) =>
                    onChange({
                      ...policy,
                      question_limit: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label className="tea-field">
                <span>最多分析轮次</span>
                <Input
                  type="number"
                  min={1}
                  max={3}
                  value={policy.max_rounds}
                  onChange={(e) =>
                    onChange({ ...policy, max_rounds: Number(e.target.value) })
                  }
                />
              </label>
              <label className="tea-field">
                <span>每个国家的检索段落数</span>
                <Input
                  type="number"
                  min={1}
                  max={20}
                  value={policy.retrieval_limit}
                  onChange={(e) =>
                    onChange({
                      ...policy,
                      retrieval_limit: Number(e.target.value),
                    })
                  }
                />
              </label>
            </div>
            <label className="tea-field">
              <span>选择规则卡</span>
              <select
                value={rule?.id ?? ""}
                onChange={(e) => setSelectedId(e.target.value)}
              >
                <option value="" disabled>
                  选择规则
                </option>
                {policy.rules.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.title || "未命名规则"}
                    {item.enabled ? "" : "（已停用）"}
                  </option>
                ))}
              </select>
            </label>
            <Button
              size="sm"
              variant="outline"
              disabled={policy.rules.length >= 60}
              onClick={() => {
                const id = makeConfigId("rule");
                onChange({
                  ...policy,
                  rules: [
                    ...policy.rules,
                    {
                      id,
                      title: "新规则",
                      module: "qa05",
                      enabled: true,
                      execution: "model",
                      domains: [],
                      jurisdictions: [],
                      condition: "",
                      instructions: "",
                      required_facts: [],
                      output_requirements: "",
                      review_required: true,
                      source_reference: "",
                    },
                  ],
                });
                setSelectedId(id);
              }}
            >
              <Plus size={14} />
              添加规则
            </Button>
            {rule && (
              <div className="tea-legal-rule">
                <label className="tea-field">
                  <span>规则名称</span>
                  <Input
                    value={rule.title}
                    maxLength={200}
                    onChange={(e) => editRule({ title: e.target.value })}
                  />
                </label>
                <label className="tea-field">
                  <span>所属分析阶段</span>
                  <select
                    value={rule.module}
                    onChange={(e) =>
                      editRule({
                        module: e.target.value as LegalRule["module"],
                      })
                    }
                  >
                    {(
                      defaults?.modules ??
                      ["qa01", "qa02", "qa03", "qa04", "qa05", "qa06"].map(
                        (id) => ({ id, title: id.toUpperCase() }),
                      )
                    ).map((module) => (
                      <option key={module.id} value={module.id}>
                        {module.title}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="tea-field">
                  <span>执行方式</span>
                  <select
                    value={rule.execution}
                    onChange={(e) =>
                      editRule({
                        execution: e.target.value as LegalRule["execution"],
                      })
                    }
                  >
                    <option value="model">模型辅助分析</option>
                    <option value="manual">人工审核</option>
                  </select>
                </label>
                <label className="tea-field">
                  <span>适用法律领域</span>
                  <ListInput
                    key={`${rule.id}:domains`}
                    value={rule.domains}
                    placeholder="以逗号分隔；留空表示通用"
                    onChange={(domains) => editRule({ domains })}
                  />
                </label>
                <label className="tea-field">
                  <span>规则适用国家</span>
                  <ListInput
                    key={`${rule.id}:jurisdictions`}
                    value={rule.jurisdictions}
                    uppercase
                    placeholder="例如 CN, ID；留空表示通用"
                    onChange={(jurisdictions) => editRule({ jurisdictions })}
                  />
                </label>
                <label className="tea-field">
                  <span>触发条件</span>
                  <Textarea
                    value={rule.condition}
                    rows={2}
                    maxLength={2000}
                    onChange={(e) => editRule({ condition: e.target.value })}
                  />
                </label>
                <label className="tea-field">
                  <span>分析要求</span>
                  <Textarea
                    value={rule.instructions}
                    rows={5}
                    maxLength={6000}
                    onChange={(e) => editRule({ instructions: e.target.value })}
                  />
                </label>
                <div className="tea-legal-requirements">
                  <h4>需要确认的事实</h4>
                  {rule.required_facts.map((fact, i) => (
                    <fieldset key={i} className="tea-legal-requirement">
                      <legend>事实 {i + 1}</legend>
                      {(
                        [
                          ["key", "事实编号"],
                          ["label", "显示名称"],
                          ["purpose", "询问原因"],
                        ] as const
                      ).map(([field, title]) => (
                        <label className="tea-field" key={field}>
                          <span>{title}</span>
                          <Input
                            value={fact[field]}
                            maxLength={
                              field === "key"
                                ? 160
                                : field === "label"
                                  ? 200
                                  : 1000
                            }
                            onChange={(e) =>
                              editRule({
                                required_facts: rule.required_facts.map(
                                  (item, at) =>
                                    at === i
                                      ? { ...item, [field]: e.target.value }
                                      : item,
                                ),
                              })
                            }
                          />
                        </label>
                      ))}
                      <label className="tea-checkbox">
                        <input
                          type="checkbox"
                          checked={fact.decisive}
                          onChange={(e) =>
                            editRule({
                              required_facts: rule.required_facts.map(
                                (item, at) =>
                                  at === i
                                    ? { ...item, decisive: e.target.checked }
                                    : item,
                              ),
                            })
                          }
                        />
                        影响结论的关键事实
                      </label>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          editRule({
                            required_facts: rule.required_facts.filter(
                              (_, at) => at !== i,
                            ),
                          })
                        }
                      >
                        <Trash2 size={13} />
                        移除此事实要求
                      </Button>
                    </fieldset>
                  ))}
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={rule.required_facts.length >= 30}
                    onClick={() =>
                      editRule({
                        required_facts: [
                          ...rule.required_facts,
                          {
                            key: makeConfigId("fact"),
                            label: "",
                            purpose: "",
                            decisive: true,
                          },
                        ],
                      })
                    }
                  >
                    <Plus size={13} />
                    添加事实要求
                  </Button>
                </div>
                <label className="tea-field">
                  <span>输出要求</span>
                  <Textarea
                    value={rule.output_requirements}
                    rows={3}
                    maxLength={2000}
                    onChange={(e) =>
                      editRule({ output_requirements: e.target.value })
                    }
                  />
                </label>
                <label className="tea-field">
                  <span>规则来源</span>
                  <Input
                    value={rule.source_reference}
                    placeholder="手册章节或已确认的法务规则"
                    maxLength={1000}
                    onChange={(e) =>
                      editRule({ source_reference: e.target.value })
                    }
                  />
                </label>
                <label className="tea-checkbox">
                  <input
                    type="checkbox"
                    checked={rule.review_required}
                    onChange={(e) =>
                      editRule({ review_required: e.target.checked })
                    }
                  />
                  额外复核
                </label>
                <p className="ta-hint">
                  法律结论始终需人工复核；此项设置额外复核要求。
                </p>
                <label className="tea-checkbox">
                  <input
                    type="checkbox"
                    checked={rule.enabled}
                    onChange={(e) => editRule({ enabled: e.target.checked })}
                  />
                  启用这条规则
                </label>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    onChange({
                      ...policy,
                      rules: policy.rules.filter((item) => item.id !== rule.id),
                    });
                    setSelectedId("");
                  }}
                >
                  <Trash2 size={13} />
                  移除规则
                </Button>
              </div>
            )}
          </fieldset>
          {defaults && (
            <details className="tea-legal-program" open>
              <summary>
                <LockKeyhole size={14} />
                程序校验 · 固定执行
              </summary>
              {defaults.program_checks.map((check) => (
                <div key={check.id}>
                  <strong>{check.title}</strong>
                  <p>{check.description}</p>
                </div>
              ))}
            </details>
          )}
          <details className="tea-legal-preview">
            <summary>用当前草稿试运行</summary>
            <p className="ta-hint">
              查看实际分析输出与依据，不创建业务记录。复杂样本也可在现有调试台保存与运行。
            </p>
            <label className="tea-field">
              <span>试运行问题</span>
              <Textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                maxLength={10000}
                rows={3}
              />
            </label>
            <ActionButton
              size="sm"
              variant="outline"
              busy={busy}
              disabled={!question.trim()}
              onClick={() => void runPreview()}
            >
              运行法律分析
            </ActionButton>
            {busy && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  previewAbort.current?.abort();
                  setBusy(false);
                }}
              >
                停止试运行
              </Button>
            )}
            {preview && <LegalResultView result={preview} />}
          </details>
        </>
      )}
    </div>
  );
}
