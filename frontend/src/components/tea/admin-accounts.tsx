"use client";

import { Search, ShieldCheck, UserRound, UsersRound } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage, patch, teaRequest } from "@/core/tea/api";
import { type ManagedUser, type ManagedUsersPage } from "@/core/tea/types";

import { Busy, Empty, ErrorNotice } from "./shared";

const PAGE_SIZE = 50;

export function AdminAccounts() {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ManagedUsersPage>({ items: [], total: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState("");
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    if (search) params.set("q", search);
    teaRequest<ManagedUsersPage>(`/admin/users?${params}`, {
      signal: abort.signal,
    })
      .then((page) => {
        if (!abort.signal.aborted) setData(page);
      })
      .catch((reason: unknown) => {
        if (!abort.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [offset, reload, search]);

  async function update(
    account: ManagedUser,
    changes: { can_login?: boolean; is_admin?: boolean },
  ) {
    if (busyId) return;
    setBusyId(account.id);
    setError("");
    try {
      const updated = await patch<ManagedUser>(
        `/admin/users/${encodeURIComponent(account.id)}`,
        { expected_revision: account.revision, ...changes },
      );
      setData((current) => ({
        ...current,
        items: current.items.map((item) =>
          item.id === updated.id ? updated : item,
        ),
      }));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusyId("");
    }
  }

  const allowedCount = data.items.filter((account) => account.can_login).length;
  const adminCount = data.items.filter(
    (account) => account.can_login && account.is_admin,
  ).length;

  return (
    <section className="ta-account-access" aria-labelledby="ta-account-title">
      <header className="ta-account-access-heading">
        <div>
          <h2 id="ta-account-title">账号权限</h2>
          <p>
            管理可登录人员和管理员。权限变更会立即生效，停用账号后会退出其现有会话。
          </p>
        </div>
        <div className="ta-account-counts" aria-label="账号概况">
          <span>
            <UsersRound size={15} /> 本页 {data.items.length} 个账号
          </span>
          <span>
            <UserRound size={15} /> 本页 {allowedCount} 个可登录
          </span>
          <span>
            <ShieldCheck size={15} /> 本页 {adminCount} 名管理员
          </span>
        </div>
      </header>

      <p className="tea-note">
        新注册账号可直接登录；可在此停用账号或调整管理员权限，至少保留一名可登录管理员。
      </p>

      <div className="ta-library-toolbar">
        <form
          className="ta-file-search"
          onSubmit={(event) => {
            event.preventDefault();
            setSearch(query.trim());
            setOffset(0);
          }}
        >
          <Search size={16} />
          <Input
            aria-label="搜索账号姓名或邮箱"
            placeholder="搜索姓名或邮箱…"
            maxLength={160}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <Button type="submit" variant="ghost" size="sm">
            搜索
          </Button>
        </form>
      </div>

      {error && (
        <ErrorNotice message={error} onRetry={() => setReload((v) => v + 1)} />
      )}

      <div className="ta-library-table">
        <div className="ta-table-heading">
          <span>{loading ? "读取中…" : `共 ${data.total} 个账号`}</span>
          {search && (
            <button
              onClick={() => {
                setSearch("");
                setQuery("");
                setOffset(0);
              }}
            >
              清除筛选
            </button>
          )}
        </div>
        {loading ? (
          <Busy label="读取账号权限…" />
        ) : error ? null : data.items.length ? (
          <div className="ta-table-scroll">
            <table>
              <thead>
                <tr>
                  <th>账号</th>
                  <th>登录权限</th>
                  <th>角色</th>
                  <th>注册时间</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((account) => (
                  <tr key={account.id}>
                    <td>
                      <div className="ta-managed-account">
                        <span>{account.name.slice(0, 1)}</span>
                        <div>
                          <strong>{account.name}</strong>
                          <small>{account.email}</small>
                        </div>
                      </div>
                    </td>
                    <td>
                      <Button
                        size="sm"
                        variant={account.can_login ? "default" : "outline"}
                        aria-pressed={account.can_login}
                        aria-label={`${account.can_login ? "关闭" : "开放"}${account.name}的登录权限`}
                        disabled={!!busyId}
                        onClick={() =>
                          void update(account, {
                            can_login: !account.can_login,
                            ...(!account.can_login ? {} : { is_admin: false }),
                          })
                        }
                      >
                        {account.can_login ? "允许登录" : "未开放"}
                      </Button>
                    </td>
                    <td>
                      <Button
                        size="sm"
                        variant={account.is_admin ? "secondary" : "outline"}
                        aria-pressed={account.is_admin}
                        aria-label={`${account.is_admin ? "取消" : "设置为"}${account.name}的管理员身份`}
                        disabled={!!busyId || !account.can_login}
                        onClick={() =>
                          void update(account, { is_admin: !account.is_admin })
                        }
                      >
                        <ShieldCheck size={14} />
                        {account.is_admin ? "管理员" : "普通人员"}
                      </Button>
                    </td>
                    <td className="ta-date-cell">
                      {dateLabel(account.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            icon={<UsersRound size={28} />}
            title={search ? "没有匹配账号" : "暂无注册账号"}
          >
            {search ? "更换关键词或清除筛选。" : "用户注册后会显示在这里。"}
          </Empty>
        )}
        {data.total > PAGE_SIZE && (
          <div className="ta-pagination">
            <span>
              第 {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} 项，共{" "}
              {data.total} 项
            </span>
            <div>
              <Button
                variant="outline"
                size="sm"
                disabled={loading || !!busyId || offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                上一页
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={
                  loading || !!busyId || offset + PAGE_SIZE >= data.total
                }
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                下一页
              </Button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function dateLabel(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? "—"
    : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium" }).format(date);
}
