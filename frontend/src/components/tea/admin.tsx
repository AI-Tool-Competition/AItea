"use client";

import {
  ArrowLeft,
  BookOpen,
  ChevronRight,
  GitBranch,
  LogOut,
  PanelLeft,
  Sparkles,
} from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { type Bootstrap, type Template } from "@/core/tea/types";

import { AdminLibrary } from "./admin-library";
import { UploadDialog } from "./library";
import { Brand } from "./shared";
import { Sources } from "./sources";
import { Studio } from "./studio";

export type AdminPage = "workflow" | "analysis" | "library" | "sources";
const pages = {
  workflow: {
    title: "办理流程",
    icon: GitBranch,
    group: "流程配置",
  },
  analysis: {
    title: "节点分析",
    icon: Sparkles,
    group: "流程配置",
  },
  library: {
    title: "资料库",
    icon: BookOpen,
    group: "内容管理",
  },
};

// Layout reference: tmp/ui dashboard-01/sidebar-07. Uses the existing Tea and
// DeerFlow primitives; no reference-project code or runtime dependency is copied.
export function Admin({
  bootstrap,
  page,
  requestedStage,
  onPublished,
  onLogout,
  logoutBusy,
}: {
  bootstrap: Bootstrap;
  page: AdminPage;
  requestedStage?: string;
  onPublished: (template: Template) => void;
  onLogout: () => void;
  logoutBusy: boolean;
}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const samples =
    process.env.NODE_ENV === "development" &&
    searchParams.get("sample") === "1";
  const [menuOpen, setMenuOpen] = useState(false);
  const [upload, setUpload] = useState<{ scope?: string; parent?: string }>();
  const [libraryRevision, setLibraryRevision] = useState(0);
  const navigationPage = page === "sources" ? "library" : page;
  const current = pages[navigationPage];
  return (
    <div
      className={`tea-theme ta-admin ${page === "workflow" || page === "analysis" ? "is-editor" : ""}`}
    >
      {menuOpen && (
        <button
          className="ta-menu-overlay"
          aria-label="关闭后台菜单"
          onClick={() => setMenuOpen(false)}
        />
      )}
      <aside
        className={`ta-admin-sidebar ${menuOpen ? "is-open" : ""}`}
        aria-label="管理后台导航"
      >
        <Link className="ta-admin-brand" href="/tea/admin/workflow">
          <Brand />
          <span className="ta-brand-tag">后台</span>
        </Link>
        <nav>
          {["流程配置", "内容管理"].map((group) => (
            <div className="ta-nav-group" key={group}>
              <span>{group}</span>
              {(
                Object.entries(pages) as [
                  keyof typeof pages,
                  (typeof pages)[keyof typeof pages],
                ][]
              )
                .filter(([, item]) => item.group === group)
                .map(([id, item]) => (
                  <Link
                    key={id}
                    href={`/tea/admin/${id}`}
                    aria-current={navigationPage === id ? "page" : undefined}
                    onClick={() => setMenuOpen(false)}
                  >
                    <item.icon size={18} strokeWidth={1.7} />
                    <span>{item.title}</span>
                    {navigationPage === id && <ChevronRight size={13} />}
                  </Link>
                ))}
            </div>
          ))}
        </nav>
        <div className="ta-sidebar-status">
          <span className="ta-mode-label">
            <span className="ta-dot" />
            {bootstrap.capabilities.assistant_mode === "live"
              ? "模型已配置"
              : "规则演示"}
          </span>
        </div>
        <Link className="ta-return-link" href="/tea">
          <ArrowLeft size={16} />
          返回工作台
        </Link>
        <div className="ta-admin-user">
          <span>{bootstrap.user.name.slice(0, 1)}</span>
          <div>
            <strong>{bootstrap.user.name}</strong>
            <small>{bootstrap.user.is_demo ? "体验账号" : "个人账号"}</small>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="退出登录"
            disabled={logoutBusy}
            onClick={onLogout}
          >
            <LogOut size={15} />
          </Button>
        </div>
      </aside>
      <div className="ta-admin-main">
        <button
          className="ta-menu-trigger"
          aria-label="展开后台菜单"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen(!menuOpen)}
        >
          <PanelLeft size={19} />
        </button>
        <main className="ta-admin-content">
          <h1 className="sr-only">{current.title}</h1>
          {navigationPage === "library" && (
            <nav className="ta-library-nav" aria-label="资料库视图">
              <Link
                href={`/tea/admin/library${samples ? "?sample=1" : ""}`}
                aria-current={page === "library" ? "page" : undefined}
              >
                资料
              </Link>
              <Link
                href={`/tea/admin/sources${samples ? "?sample=1" : ""}`}
                aria-current={page === "sources" ? "page" : undefined}
              >
                来源
              </Link>
              {samples && (
                <span className="ta-sample-label">开发样例 · 仅预览</span>
              )}
              {samples ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => router.replace(`/tea/admin/${page}`)}
                >
                  退出样例
                </Button>
              ) : process.env.NODE_ENV === "development" ? (
                <Button asChild variant="ghost" size="sm">
                  <Link
                    href={`/tea/admin/${page}?sample=1`}
                    target="_blank"
                    rel="noreferrer"
                    title="在新标签页查看开发样例"
                  >
                    开发样例
                  </Link>
                </Button>
              ) : null}
            </nav>
          )}
          {page === "workflow" || page === "analysis" ? (
            <Studio
              ownerId={bootstrap.user.id}
              templates={bootstrap.templates}
              onPublished={onPublished}
              page={page}
              requestedStage={requestedStage}
              onPage={(next, stage) =>
                router.push(
                  `/tea/admin/${next}${stage ? `?stage=${encodeURIComponent(stage)}` : ""}`,
                )
              }
            />
          ) : page === "library" ? (
            <AdminLibrary
              key={`${bootstrap.user.id}:${samples}`}
              ownerId={bootstrap.user.id}
              cases={bootstrap.cases}
              revision={libraryRevision}
              samples={samples}
              onUpload={(scope, parent) => setUpload({ scope, parent })}
            />
          ) : (
            <Sources
              key={`${bootstrap.user.id}:${samples}`}
              ownerId={bootstrap.user.id}
              compact
              samples={samples}
            />
          )}
        </main>
      </div>
      <UploadDialog
        open={!!upload}
        onOpenChange={(open) => {
          if (!open) setUpload(undefined);
        }}
        caseId={upload?.scope}
        initialParentId={upload?.parent}
        cases={bootstrap.cases}
        onDone={() => setLibraryRevision((value) => value + 1)}
      />
    </div>
  );
}
