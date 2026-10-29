"use client";
import { CheckCircle2, FileText, Save, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { errorMessage, patch, post } from "@/core/tea/api";
import {
  type Artifact,
  type CaseDetail,
  type TaskState,
} from "@/core/tea/types";

import { ActionButton, Checks, ErrorNotice, taskStatus } from "./shared";

interface TaskDraft {
  values: TaskState["values"];
  evidence: string[];
  baseValues: TaskState["values"];
  baseEvidence: string[];
  revision: number;
}
const taskDrafts = new Map<string, TaskDraft>();
export function clearTaskDrafts() {
  taskDrafts.clear();
}

export function TaskPanel({
  task,
  record,
  locked,
  onUpdate,
  onArtifact,
  onUpload,
}: {
  task: TaskState;
  record: CaseDetail;
  locked: boolean;
  onUpdate: (record: CaseDetail) => void;
  onArtifact: (artifact: Artifact) => void | Promise<void>;
  onUpload: () => void;
}) {
  const draftKey = `${record.id}:${task.id}`;
  const cached = taskDrafts.get(draftKey);
  const [values, setValues] = useState(cached?.values ?? task.values);
  const [evidence, setEvidence] = useState(
    cached?.evidence ?? task.evidence_ids,
  );
  const [baseline, setBaseline] = useState({
    values: cached?.baseValues ?? task.values,
    evidence: cached?.baseEvidence ?? task.evidence_ids,
    revision: cached?.revision ?? task.revision,
  });
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const pending = useRef(false);
  const changed =
    JSON.stringify(values) !== JSON.stringify(baseline.values) ||
    JSON.stringify(evidence) !== JSON.stringify(baseline.evidence);
  useEffect(() => {
    if (changed)
      taskDrafts.set(draftKey, {
        values,
        evidence,
        baseValues: baseline.values,
        baseEvidence: baseline.evidence,
        revision: baseline.revision,
      });
    else taskDrafts.delete(draftKey);
  }, [draftKey, values, evidence, baseline, changed]);
  useEffect(() => {
    if (task.revision !== baseline.revision && !changed && !busy) {
      setValues(task.values);
      setEvidence(task.evidence_ids);
      setBaseline({
        values: task.values,
        evidence: task.evidence_ids,
        revision: task.revision,
      });
      setConfirmed(false);
    }
  }, [task, baseline.revision, changed, busy]);
  async function act(action: "save" | "verify" | "draft") {
    if (pending.current) return;
    pending.current = true;
    setBusy(action);
    setError("");
    try {
      let next = record;
      if (changed) {
        next = await patch<CaseDetail>(`/cases/${record.id}/tasks/${task.id}`, {
          values,
          evidence_ids: evidence,
          expected_revision: baseline.revision,
        });
        const savedTask = next.stages
          .flatMap((s) => s.tasks)
          .find((t) => t.id === task.id);
        if (savedTask)
          setBaseline({
            values: savedTask.values,
            evidence: savedTask.evidence_ids,
            revision: savedTask.revision,
          });
        taskDrafts.delete(draftKey);
        onUpdate(next);
      }
      if (action === "draft") {
        const artifact = await post<Artifact>(`/cases/${record.id}/artifacts`, {
          task_id: task.id,
        });
        await onArtifact(artifact);
      } else {
        if (action === "verify") {
          const current = next.stages
            .flatMap((s) => s.tasks)
            .find((t) => t.id === task.id);
          if (!current) throw new Error("任务已变更，请刷新业务。");
          next = await post<CaseDetail>(
            `/cases/${record.id}/tasks/${task.id}/verify`,
            { expected_revision: current.revision, confirm: confirmed },
          );
        }
        onUpdate(next);
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      pending.current = false;
      setBusy("");
    }
  }
  return (
    <div className="tea-task-detail">
      <div className="tea-task-heading">
        <span className={`tea-status ${task.status}`}>
          {task.needs_review ? "需要复核" : taskStatus[task.status]}
        </span>
        <span>
          {task.required
            ? task.blocks_next_stage === false
              ? "结案前必办 · 可与后续阶段并行"
              : "必办任务"
            : "选办任务"}
        </span>
      </div>
      <h3>{task.title}</h3>
      <p className="tea-muted">{task.description}</p>
      {locked && (
        <div className="tea-note">
          完成前一阶段后可办理本任务，目前可以预览要求。
        </div>
      )}
      {changed && baseline.revision !== task.revision && (
        <div className="tea-note">
          资料已有更新。你的未保存输入已保留，系统会阻止覆盖。
          <button
            type="button"
            onClick={() => {
              setValues(task.values);
              setEvidence(task.evidence_ids);
              setBaseline({
                values: task.values,
                evidence: task.evidence_ids,
                revision: task.revision,
              });
              setConfirmed(false);
              setError("");
            }}
          >
            放弃本地修改，载入最新资料
          </button>
        </div>
      )}
      <div className="tea-form-fields">
        {task.fields.map((field) => (
          <div key={field.key}>
            <label className="tea-field">
              <span>
                {field.label}
                {field.required && <b> *</b>}
              </span>
              {field.type === "select" ? (
                <select
                  value={values[field.key] ?? ""}
                  disabled={locked || !!busy}
                  onChange={(e) => {
                    setValues({ ...values, [field.key]: e.target.value });
                    setConfirmed(false);
                  }}
                >
                  <option value="">请选择</option>
                  {field.options?.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ) : field.type === "textarea" ? (
                <Textarea
                  value={values[field.key] ?? ""}
                  disabled={locked || !!busy}
                  placeholder={field.placeholder}
                  onChange={(e) => {
                    setValues({ ...values, [field.key]: e.target.value });
                    setConfirmed(false);
                  }}
                />
              ) : (
                <Input
                  type={field.type}
                  min={field.min}
                  step={field.type === "number" ? "any" : undefined}
                  value={values[field.key] ?? ""}
                  disabled={locked || !!busy}
                  placeholder={field.placeholder}
                  onChange={(e) => {
                    setValues({
                      ...values,
                      [field.key]:
                        field.type === "number" && e.target.value !== ""
                          ? Number(e.target.value)
                          : e.target.value,
                    });
                    setConfirmed(false);
                  }}
                />
              )}
            </label>
            {task.field_sources?.[field.key] &&
              values[field.key] === task.values[field.key] && (
                <details className="tea-small-note">
                  <summary>
                    茶渡根据对话填写 · {task.confirmed ? "已确认" : "待核对"}
                  </summary>
                  <p>来源：{task.field_sources[field.key]!.quote}</p>
                </details>
              )}
          </div>
        ))}
      </div>
      <div className="tea-evidence-heading">
        <span>关联材料{task.require_document ? " · 必需" : " · 可选"}</span>
        <button onClick={onUpload}>上传材料</button>
      </div>
      {record.documents.filter((d) => d.role === "primary").length ? (
        <div className="tea-evidence-list">
          {record.documents
            .filter((d) => d.role === "primary")
            .map((doc) => (
              <label key={doc.id}>
                <input
                  type="checkbox"
                  disabled={locked || !!busy || doc.status !== "ready"}
                  checked={evidence.includes(doc.id)}
                  onChange={(e) => {
                    setEvidence(
                      e.target.checked
                        ? [...evidence, doc.id]
                        : evidence.filter((id) => id !== doc.id),
                    );
                    setConfirmed(false);
                  }}
                />
                <FileText size={15} />
                <span>
                  {doc.title || doc.filename}
                  <small>
                    v{doc.version} ·{" "}
                    {doc.status === "ready" ? "可核验" : "暂不可核验"}
                  </small>
                </span>
              </label>
            ))}
        </div>
      ) : (
        <p className="tea-small-note">还没有本次业务的材料，可先上传再关联。</p>
      )}
      {task.require_confirmation && (
        <label className="tea-confirm">
          <input
            type="checkbox"
            checked={confirmed}
            disabled={locked || !!busy}
            onChange={(e) => setConfirmed(e.target.checked)}
          />
          <span>我确认以上信息和关联材料适用于本批次业务。</span>
        </label>
      )}
      {error && <ErrorNotice message={error} />}
      <div className="tea-task-actions">
        <ActionButton
          variant="outline"
          size="sm"
          busy={busy === "save"}
          disabled={locked || !!busy || !changed}
          onClick={() => act("save")}
        >
          <Save size={14} />
          保存
        </ActionButton>
        <ActionButton
          size="sm"
          busy={busy === "verify"}
          disabled={locked || !!busy}
          onClick={() => act("verify")}
        >
          <ShieldCheck size={14} />
          提交核验
        </ActionButton>
      </div>
      {task.verification && (
        <section className="tea-verification">
          <h4>
            <CheckCircle2 size={15} />
            最近一次核验
          </h4>
          <Checks checks={task.verification.checks} />
        </section>
      )}
      <Button
        variant="outline"
        className="tea-draft-button"
        disabled={locked || !!busy}
        onClick={() => act("draft")}
      >
        <FileText size={15} />
        {busy === "draft" ? "正在起草…" : "根据当前资料起草文稿"}
      </Button>
    </div>
  );
}
