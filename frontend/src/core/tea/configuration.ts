import { z } from "zod";

import { type AgentStep, type Template } from "./types";

// Drafts may be incomplete. Publication and test runs use the server's stricter
// validation, while import rejects unknown rules instead of silently losing them.
const optionalText = z
  .string()
  .nullish()
  .transform((value) => value ?? undefined);
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
    placeholder: optionalText,
  })
  .strict();
const taskSchema = z
  .object({
    id: z.string(),
    title: z.string(),
    description: z.string(),
    required: z.boolean(),
    fields: z.array(fieldSchema).max(30),
    require_document: z.boolean(),
    require_confirmation: z.boolean(),
    output_template: optionalText,
  })
  .strict();
const agentSchema = z
  .object({
    language: z.string(),
    instructions: z.string(),
    retrieval_limit: z.number(),
    steps: z
      .array(
        z
          .object({
            id: z.string(),
            type: z.enum(["retrieve", "check", "guide", "draft", "verify"]),
            label: z.string(),
            enabled: z.boolean(),
            instructions: z.string().optional(),
          })
          .strict(),
      )
      .max(20),
  })
  .strict();
export const configurationSchema = z
  .object({
    id: z.string().regex(/^[a-z][a-z0-9_-]{0,63}$/),
    version: z.number().int().min(1),
    title: z.string(),
    description: z.string(),
    origin: z.string(),
    destination: z.string(),
    is_demo: z.boolean(),
    modes: z
      .array(
        z
          .object({
            id: z.string(),
            name: z.string(),
            description: z.string(),
            enabled: z.boolean(),
          })
          .strict(),
      )
      .max(20),
    stages: z
      .array(
        z
          .object({
            id: z.string(),
            name: z.string(),
            actor: z.string(),
            description: z.string(),
            tasks: z.array(taskSchema).max(30),
            analysis: agentSchema.nullish(),
          })
          .strict(),
      )
      .min(1)
      .max(20),
    agent: agentSchema,
  })
  .strict();

export const stepTypes: Record<
  AgentStep["type"],
  { label: string; description: string }
> = {
  retrieve: {
    label: "查找资料",
    description: "从已审核、可用的一级资料中查找原文依据。",
  },
  check: {
    label: "检查资料",
    description: "按本节点任务的字段、材料与确认要求检查缺项。",
  },
  guide: {
    label: "分析与建议",
    description: "根据前面步骤的检查与引用，给出接下来的办理建议。",
  },
  draft: {
    label: "起草提示",
    description: "提示用户到任务中起草文稿；分析时不自动保存文稿。",
  },
  verify: {
    label: "核验提示",
    description: "提示用户核验；分析结果不会把任务标记为完成。",
  },
};
export function makeConfigId(prefix: string) {
  return `${prefix}_${crypto.randomUUID().slice(0, 8)}`;
}
export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (from < 0 || to < 0 || from >= items.length || to >= items.length)
    return items;
  const next = [...items];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item!);
  return next;
}
const fieldNames: Record<string, string> = {
  title: "任务名称",
  name: "名称",
  actor: "办理主体",
  description: "说明",
  fields: "填写项",
  key: "字段编号",
  label: "显示名称",
  type: "类型",
  tasks: "任务",
  stages: "办理节点",
  modes: "办理模式",
  steps: "分析步骤",
  instructions: "分析要求",
  language: "输出语言",
  retrieval_limit: "引用数量",
  min: "最小值",
  origin: "出发国家",
  destination: "目的国家",
  agent: "默认分析流程",
  analysis: "专用分析流程",
  id: "编号",
  version: "版本",
  required: "必填要求",
};
export function configurationIssueLabel(path: string, template: Template) {
  const parts = path.split(".");
  const labels: string[] = [];
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i]!;
    if (part === "stages" && /^\d+$/.test(parts[i + 1] ?? "")) {
      const index = Number(parts[++i]);
      const name = template.stages[index]?.name;
      labels.push(name?.length ? name : `节点 ${index + 1}`);
    } else if (part === "tasks" && /^\d+$/.test(parts[i + 1] ?? "")) {
      labels.push(`任务 ${Number(parts[++i]) + 1}`);
    } else if (/^\d+$/.test(part)) labels.push(`第 ${Number(part) + 1} 项`);
    else labels.push(fieldNames[part] ?? part);
  }
  return labels.filter(Boolean).join(" › ") || "整个配置";
}
