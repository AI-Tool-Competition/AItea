import { countryLabel } from "@/core/tea/countries";

const labels: Record<string, string> = {
  query: "检索内容",
  scope: "适用范围",
  limit: "最多引用段数",
  question: "问题",
  tasks: "任务资料",
  title: "名称",
  label: "填写项",
  values: "实际填写",
  fields: "字段规则",
  required: "必填",
  min: "最小值",
  type: "类型",
  placeholder: "填写提示",
  require_document: "需要关联材料",
  require_confirmation: "需要人工确认",
  confirmed: "已人工确认",
  citations: "原文依据",
  excerpt: "原文片段",
  checks: "检查结果",
  reason: "说明",
  status: "状态",
  mode: "检索方式",
  content: "内容",
  notice: "说明",
  case_context: "业务资料",
  stage: "办理节点",
  origin: "出发地",
  destination: "目的地",
  test_input: "样例问题",
  previous_step_output: "上一步的建议",
  instructions: "整体分析要求",
  step_instructions: "本步分析要求",
  language: "输出语言",
  instructions_applied: "分析要求已用于模型",
  document_version: "资料版本",
  metadata: "资料信息",
  truncated: "内容已按上限截取",
  summary: "摘要",
  task_title: "任务",
  filename: "文件名",
  reviewed_at: "审核时间",
  classification: "分类",
  jurisdiction: "适用地区",
  valid_from: "生效日期",
  valid_until: "失效日期",
  review_status: "审核状态",
  reference_status: "适用状态",
  source_url: "来源",
  document_ids: "限定资料",
  country: "资料国家",
  evidence_ids: "关联材料",
  evidence_refs: "依据",
  score: "相关程度",
};
const hidden = new Set([
  "id",
  "task_id",
  "document_id",
  "key",
  "metadata_revision",
  "locator",
  "content_hash",
  "family_id",
  "effective_version_id",
]);
export const valueLabels: Record<string, string> = {
  passed: "通过",
  missing: "待补充",
  conflict: "有冲突",
  lexical: "关键词检索",
  hybrid: "混合检索",
  ragflow: "RAGFlow 混合检索",
  unavailable: "检索未就绪",
  number: "数字",
  text: "短文字",
  textarea: "多行文字",
  approved: "已审核",
  draft: "待审核",
  active: "可用",
  superseded: "已被替代",
  law: "法规",
  guidance: "指南",
  manual: "人工确认",
};
export function ReadableData({
  value,
  depth = 0,
}: {
  value: unknown;
  depth?: number;
}) {
  if (value === null || value === undefined)
    return <span className="ta-hint">未设置</span>;
  if (typeof value === "boolean") return <span>{value ? "是" : "否"}</span>;
  if (typeof value === "string" || typeof value === "number")
    return (
      <span className="ta-debug-value">
        {valueLabels[String(value)] ??
          (value === "" ? "未填写" : String(value))}
      </span>
    );
  if (Array.isArray(value))
    return value.length ? (
      <div className="ta-debug-data-list">
        {value.map((item, index) => (
          <div key={index}>
            <ReadableData value={item} depth={depth + 1} />
          </div>
        ))}
      </div>
    ) : (
      <span className="ta-hint">无</span>
    );
  if (typeof value !== "object") return <span>未设置</span>;
  const record = value as Record<string, unknown>;
  if (depth > 5) return <span className="ta-hint">详见原始数据</span>;
  if (
    typeof record.label === "string" &&
    typeof record.status === "string" &&
    ["passed", "missing", "conflict"].includes(record.status)
  ) {
    return (
      <div className="ta-debug-check-row" data-status={record.status}>
        <strong>{record.label}</strong>
        <span>{valueLabels[record.status]}</span>
        <small>
          {typeof record.task_title === "string" && `${record.task_title} · `}
          {typeof record.reason === "string" && record.reason}
        </small>
      </div>
    );
  }
  if (
    Array.isArray(record.fields) &&
    record.values &&
    typeof record.values === "object"
  ) {
    const fields = record.fields as Array<{
      key: string;
      label: string;
      required?: boolean;
      min?: number;
    }>;
    const values = record.values as Record<string, unknown>;
    return (
      <div className="ta-debug-task-data">
        <strong>
          {typeof record.title === "string" ? record.title : "任务"}
        </strong>
        <dl>
          {fields.map((field) => (
            <div key={field.key}>
              <dt>
                {field.label}
                {field.required ? "（必填）" : ""}
                {field.min !== null && field.min !== undefined
                  ? ` ≥ ${field.min}`
                  : ""}
              </dt>
              <dd>
                <ReadableData value={values[field.key] ?? "未填写"} />
              </dd>
            </div>
          ))}
        </dl>
        {record.require_document === true && (
          <p className="ta-hint">需要关联材料</p>
        )}
        {record.require_confirmation === true && (
          <p className="ta-hint">需要人工确认</p>
        )}
      </div>
    );
  }
  const entries = Object.entries(record).filter(([key]) => !hidden.has(key));
  return (
    <dl className="ta-debug-data">
      {entries.map(([key, item]) => (
        <div key={key}>
          <dt>{labels[key] ?? key}</dt>
          <dd>
            {key === "country" ? (
              <span>
                {countryLabel(typeof item === "string" ? item : "unassigned")}
              </span>
            ) : ["document_ids", "evidence_ids", "evidence_refs"].includes(
                key,
              ) ? (
              <span>
                {Array.isArray(item) ? `${item.length} 份` : "当前资料库"}
              </span>
            ) : (
              <ReadableData value={item} depth={depth + 1} />
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}
