"use client";
import { Clock3, Globe2, Plus, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage, patch, post, teaRequest } from "@/core/tea/api";
import { type ListResult, type Source } from "@/core/tea/types";

import {
  ActionButton,
  Busy,
  dateLabel,
  Empty,
  ErrorNotice,
  SectionHeading,
} from "./shared";

export function Sources() {
  const [sources, setSources] = useState<Source[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [interval, setInterval] = useState(24);
  const [tags, setTags] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const pending = useRef(false);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    teaRequest<ListResult<Source>>("/sources", { signal: abort.signal })
      .then((result) => {
        setSources(result.items);
        setError("");
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [revision]);
  async function add() {
    if (pending.current) return;
    pending.current = true;
    setBusy("add");
    setError("");
    try {
      const source = await post<Source>("/sources", {
        name,
        url,
        enabled: true,
        interval_hours: interval,
        tags: tags
          .split(/[,，]/)
          .map((t) => t.trim())
          .filter(Boolean),
      });
      setSources((s) => [...s, source]);
      setAdding(false);
      setName("");
      setUrl("");
      setTags("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      pending.current = false;
      setBusy("");
    }
  }
  async function mutate(source: Source, fetch: boolean) {
    if (pending.current) return;
    pending.current = true;
    setBusy(source.id);
    setError("");
    setNotice("");
    try {
      let next: Source;
      if (fetch) {
        const result = await post<{ source: Source; documents: unknown[] }>(
          `/sources/${source.id}/fetch`,
        );
        next = result.source;
        if (result.source.status === "failed" || result.source.last_error)
          setError(
            `抓取未全部完成。已保存 ${result.documents.length} 份文件。${result.source.last_error ?? "请检查来源后重试。"}`,
          );
        else
          setNotice(
            `抓取已完成，保存 ${result.documents.length} 份文件；相同内容会自动去重。`,
          );
      } else
        next = await patch<Source>(`/sources/${source.id}`, {
          enabled: !source.enabled,
        });
      setSources((all) => all.map((s) => (s.id === next.id ? next : s)));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      pending.current = false;
      setBusy("");
    }
  }
  return (
    <div className="tea-page tea-sources">
      <SectionHeading title="来源管理">
        <Button onClick={() => setAdding(!adding)}>
          <Plus size={15} />
          添加来源
        </Button>
      </SectionHeading>
      <p className="tea-page-intro">
        配置固定的公开 HTTPS 来源，保存主文件及关联附件，保留抓取记录。
      </p>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((r) => r + 1)}
        />
      )}
      {notice && (
        <div className="tea-success" role="status">
          {notice}
        </div>
      )}
      {adding && (
        <form
          className="tea-source-form"
          onSubmit={(e) => {
            e.preventDefault();
            void add();
          }}
        >
          <div className="tea-form-row">
            <label className="tea-field">
              <span>来源名称</span>
              <Input
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="例如：官方法规发布页面"
              />
            </label>
            <label className="tea-field">
              <span>检查间隔 · 小时</span>
              <Input
                required
                type="number"
                min={1}
                max={8760}
                value={interval}
                onChange={(e) => setInterval(Number(e.target.value))}
              />
            </label>
          </div>
          <label className="tea-field">
            <span>公开 HTTPS 地址</span>
            <Input
              type="url"
              required
              pattern="https://.*"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://…"
            />
          </label>
          <label className="tea-field">
            <span>标签</span>
            <Input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="法规, 办事指南"
            />
          </label>
          <ActionButton busy={busy === "add"} disabled={!!busy} type="submit">
            保存来源
          </ActionButton>
        </form>
      )}
      {loading ? (
        <Busy />
      ) : sources.length ? (
        <div className="tea-source-list">
          {sources.map((source) => (
            <article key={source.id}>
              <div className="tea-source-top">
                <span className="tea-source-icon">
                  <Globe2 size={22} />
                </span>
                <div>
                  <h3>{source.name}</h3>
                  <p>{source.url}</p>
                </div>
                <label className="tea-checkbox">
                  <input
                    type="checkbox"
                    checked={source.enabled}
                    disabled={!!busy}
                    onChange={() => mutate(source, false)}
                  />
                  启用
                </label>
              </div>
              <div className="tea-source-meta">
                <span>
                  <Clock3 size={13} />每 {source.interval_hours} 小时检查
                </span>
                <span>
                  {source.last_fetched_at
                    ? `上次 ${dateLabel(source.last_fetched_at)}`
                    : "尚未抓取"}
                </span>
                <ActionButton
                  variant="outline"
                  size="sm"
                  busy={busy === source.id}
                  disabled={!!busy}
                  onClick={() => mutate(source, true)}
                >
                  <RefreshCw size={13} />
                  立即抓取
                </ActionButton>
              </div>
              {source.last_error && <ErrorNotice message={source.last_error} />}
            </article>
          ))}
        </div>
      ) : (
        <Empty icon={<Globe2 size={34} />} title="从可靠的来源开始">
          添加法学同学确认过的官方页面或文件地址。
        </Empty>
      )}
      <div className="tea-note">
        一级文件解析并建立检索索引，二级附件仅存原文。后台采集进程启动后，按配置的时间间隔检查更新。
      </div>
    </div>
  );
}
