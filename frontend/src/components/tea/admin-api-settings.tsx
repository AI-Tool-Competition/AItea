"use client";

import { Save } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { errorMessage, getApiSettings, saveApiSettings } from "@/core/tea/api";
import {
  type ApiModelProvider,
  type ApiSettings,
  type ApiSettingsPatch,
} from "@/core/tea/types";

import { Busy, ErrorNotice } from "./shared";

const providers: {
  id: ApiModelProvider;
  title: string;
  description: string;
}[] = [
  { id: "agent", title: "Agent", description: "用于对话调度与法律分析。" },
  {
    id: "embedding",
    title: "Embedding",
    description: "用于资料向量化与检索，由 RAGFlow 调用。",
  },
  {
    id: "rerank",
    title: "Rerank",
    description: "用于检索结果重排，由 RAGFlow 调用。",
  },
];
const ids = ["agent", "embedding", "rerank", "mineru"] as const;
type ProviderId = (typeof ids)[number];
interface ProviderDraft {
  base_url: string;
  model: string;
  api_key: string;
  clear_api_key: boolean;
}
type Draft = Record<ProviderId, ProviderDraft>;

function draftFrom(settings: ApiSettings): Draft {
  return Object.fromEntries(
    ids.map((id) => [
      id,
      {
        base_url: id === "mineru" ? "" : settings[id].base_url,
        model: id === "mineru" ? "" : settings[id].model,
        api_key: "",
        clear_api_key: false,
      },
    ]),
  ) as Draft;
}

function changesFrom(settings: ApiSettings, draft: Draft): ApiSettingsPatch {
  const input: ApiSettingsPatch = { expected_revision: settings.revision };
  for (const id of ids) {
    const changed: NonNullable<ApiSettingsPatch["agent"]> = {};
    if (id !== "mineru") {
      for (const field of ["base_url", "model"] as const) {
        if (draft[id][field] !== settings[id][field])
          changed[field] = draft[id][field].trim();
      }
    }
    if (draft[id].clear_api_key) changed.clear_api_key = true;
    else if (draft[id].api_key.trim())
      changed.api_key = draft[id].api_key.trim();
    if (Object.keys(changed).length) input[id] = changed;
  }
  return input;
}

export function AdminApiSettings() {
  const router = useRouter();
  const [settings, setSettings] = useState<ApiSettings>();
  const [draft, setDraft] = useState<Draft>();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reload, setReload] = useState(0);
  const [leaveTo, setLeaveTo] = useState("");
  const latest = useRef({ settings, draft });
  latest.current = { settings, draft };
  const saveRequest = useRef<AbortController | undefined>(undefined);
  const dirty =
    !!settings &&
    !!draft &&
    Object.keys(changesFrom(settings, draft)).length > 1;

  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    setNotice("");
    getApiSettings(abort.signal)
      .then((next) => {
        if (abort.signal.aborted) return;
        const previous = latest.current;
        const refreshed = draftFrom(next);
        if (previous.settings && previous.draft) {
          const edits = changesFrom(previous.settings, previous.draft);
          for (const id of ids)
            refreshed[id] = { ...refreshed[id], ...edits[id] };
          setNotice("已读取最新配置，未保存的输入已保留。请核对后保存。");
        }
        setSettings(next);
        setDraft(refreshed);
      })
      .catch((reason: unknown) => {
        if (!abort.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [reload]);

  useEffect(() => () => saveRequest.current?.abort(), []);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const guard = (event: MouseEvent) => {
      if (
        event.button !== 0 ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        event.altKey
      )
        return;
      const link =
        event.target instanceof Element ? event.target.closest("a") : null;
      if (
        !(link instanceof HTMLAnchorElement) ||
        link.target === "_blank" ||
        link.hasAttribute("download")
      )
        return;
      if (link.href === window.location.href) return;
      event.preventDefault();
      event.stopPropagation();
      setLeaveTo(link.href);
    };
    window.addEventListener("beforeunload", warn);
    document.addEventListener("click", guard, true);
    return () => {
      window.removeEventListener("beforeunload", warn);
      document.removeEventListener("click", guard, true);
    };
  }, [dirty]);

  function edit(id: ProviderId, changes: Partial<ProviderDraft>) {
    setDraft((current) =>
      current ? { ...current, [id]: { ...current[id], ...changes } } : current,
    );
    setNotice("");
  }

  async function save() {
    if (!settings || !draft || saveRequest.current || !dirty) return;
    const abort = new AbortController();
    saveRequest.current = abort;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const next = await saveApiSettings(
        changesFrom(settings, draft),
        abort.signal,
      );
      if (abort.signal.aborted) return;
      setSettings(next);
      setDraft(draftFrom(next));
      setNotice(
        "配置已保存。修改检索模型后，请停止本实例后端及采集服务，同步 RAGFlow 后重新启动。",
      );
    } catch (reason) {
      if (!abort.signal.aborted) setError(errorMessage(reason));
    } finally {
      saveRequest.current = undefined;
      if (!abort.signal.aborted) setSaving(false);
    }
  }

  function keyField(id: ProviderId, title: string) {
    if (!settings || !draft) return null;
    const locked = settings.environment_overrides.includes(
      id === "mineru" ? "MINERU_API_TOKEN" : `${id.toUpperCase()}_API_KEY`,
    );
    const configured = settings[id].api_key_configured;
    return (
      <div className="ta-api-key-field">
        <label className="tea-field" htmlFor={`${id}-api-key`}>
          <span>
            API Key{" "}
            <small>
              {configured
                ? "已配置"
                : (id === "agent" && settings.agent_inherits_api_key) ||
                    (id === "embedding" && settings.embedding_inherits_api_key)
                  ? "继承通用模型密钥"
                  : "未配置"}
            </small>
          </span>
          <Input
            id={`${id}-api-key`}
            aria-label={`${title} API Key`}
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            maxLength={4096}
            placeholder={
              locked ? "由启动环境固定" : "留空保留原值；输入新密钥以替换"
            }
            value={draft[id].api_key}
            disabled={locked || draft[id].clear_api_key}
            onChange={(event) => edit(id, { api_key: event.target.value })}
          />
        </label>
        <label className="tea-checkbox">
          <input
            type="checkbox"
            aria-label={`清除 ${title} API Key`}
            checked={draft[id].clear_api_key}
            disabled={locked}
            onChange={(event) =>
              edit(id, {
                clear_api_key: event.target.checked,
                ...(event.target.checked ? { api_key: "" } : {}),
              })
            }
          />
          保存时清除密钥
        </label>
      </div>
    );
  }

  return (
    <section className="ta-api-settings" aria-labelledby="ta-api-title">
      <header className="ta-account-access-heading">
        <div>
          <h2 id="ta-api-title">API 配置</h2>
          <p>
            管理本实例的 Agent、Embedding、Rerank 和 MinerU
            接口。密钥仅保存在后端，页面不回显原值。
          </p>
        </div>
      </header>
      <p className="tea-note">
        保存后重启本实例后端及采集服务生效。Embedding 与 Rerank 需停服同步到
        RAGFlow 后重新启动；保存配置不会发起模型调用。
      </p>
      {settings?.restart_required && (
        <p className="ta-api-pending" role="status">
          已保存配置与当前运行配置不同，等待重启生效。
        </p>
      )}
      {!!settings?.environment_overrides.length && (
        <p className="tea-note">部分字段由启动环境固定，需在启动环境中修改。</p>
      )}
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setReload((value) => value + 1)}
        />
      )}
      {notice && (
        <p className="ta-api-notice" role="status">
          {notice}
        </p>
      )}
      {loading && !settings ? (
        <Busy label="读取 API 配置…" />
      ) : settings && draft ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <fieldset disabled={saving || loading} className="ta-api-cards">
            {providers.map(({ id, title, description }) => (
              <section
                className="ta-api-card"
                key={id}
                aria-labelledby={`${id}-title`}
              >
                <h3 id={`${id}-title`}>{title}</h3>
                <p>{description}</p>
                <div className="ta-api-model-fields">
                  <label className="tea-field" htmlFor={`${id}-base-url`}>
                    <span>Base URL</span>
                    <Input
                      id={`${id}-base-url`}
                      aria-label={`${title} Base URL`}
                      type="url"
                      maxLength={2048}
                      spellCheck={false}
                      autoComplete="off"
                      value={draft[id].base_url}
                      disabled={settings.environment_overrides.includes(
                        `${id.toUpperCase()}_BASE_URL`,
                      )}
                      placeholder={
                        id === "agent" || id === "embedding"
                          ? "留空继承通用模型地址"
                          : "https://api.example.com/v1"
                      }
                      onChange={(event) =>
                        edit(id, { base_url: event.target.value })
                      }
                    />
                  </label>
                  <label className="tea-field" htmlFor={`${id}-model`}>
                    <span>调用模型</span>
                    <Input
                      id={`${id}-model`}
                      aria-label={`${title} 调用模型`}
                      maxLength={200}
                      spellCheck={false}
                      autoComplete="off"
                      value={draft[id].model}
                      disabled={settings.environment_overrides.includes(
                        `${id.toUpperCase()}_MODEL`,
                      )}
                      placeholder="填写供应商提供的模型名称"
                      onChange={(event) =>
                        edit(id, { model: event.target.value })
                      }
                    />
                  </label>
                </div>
                {keyField(id, title)}
                {id === "agent" && (
                  <p className="ta-api-help">
                    地址和密钥留空时继承通用模型配置，模型留空时使用现有默认值。
                  </p>
                )}
                {id === "rerank" && (
                  <p className="ta-api-help">
                    不使用重排时，同时清空地址、模型和密钥，再同步 RAGFlow。
                  </p>
                )}
              </section>
            ))}
            <section className="ta-api-card" aria-labelledby="mineru-title">
              <h3 id="mineru-title">MinerU</h3>
              <p>用于 PDF、Office 与图片的云端精准解析。</p>
              {keyField("mineru", "MinerU")}
            </section>
          </fieldset>
          <div className="ta-api-actions">
            <span>{dirty ? "有未保存的修改" : "配置已读取"}</span>
            <Button type="submit" disabled={!dirty || saving || loading}>
              <Save size={15} />
              {saving ? "正在保存…" : "保存配置"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={saving || loading}
              onClick={() => setReload((value) => value + 1)}
            >
              读取最新版本
            </Button>
          </div>
          <details className="ta-api-operations">
            <summary>配置生效步骤</summary>
            <p>
              修改 Embedding 或 Rerank
              后，先停止本实例后端及所属采集服务，在当前实例目录执行{" "}
              <code>make ragflow-bootstrap</code>{" "}
              同步模型，再启动本实例后端与所属采集服务。尚未部署 RAGFlow
              时，先按接入文档完成部署。
            </p>
            <p>
              只修改 Agent 或 MinerU 时，重启本实例后端与所属采集服务即可。清除
              Agent 密钥后，若仍有通用模型密钥，会继续继承它。
            </p>
          </details>
        </form>
      ) : null}
      <Dialog
        open={!!leaveTo}
        onOpenChange={(open) => {
          if (!open) setLeaveTo("");
        }}
      >
        <DialogContent className="tea-theme tea-dialog">
          <DialogHeader>
            <DialogTitle>离开 API 配置？</DialogTitle>
            <DialogDescription>
              当前修改尚未保存，离开后会丢失本页输入的内容和新密钥。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setLeaveTo("")}>
              继续编辑
            </Button>
            <Button
              onClick={() => {
                const target = leaveTo;
                setLeaveTo("");
                router.push(target);
              }}
            >
              放弃修改并离开
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
