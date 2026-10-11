"use client";
import {
  ArrowRight,
  ChevronRight,
  LogOut,
  Plus,
  ShieldCheck,
  Sprout,
  UserRound,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { errorMessage, post, TeaApiError, teaRequest } from "@/core/tea/api";
import {
  type Bootstrap,
  type CaseDetail,
  type Template,
  type User,
} from "@/core/tea/types";

import { Admin, type AdminPage } from "./admin";
import { clearArtifactDrafts } from "./artifact-panel";
import { Library, UploadDialog } from "./library";
import { TeaNavigation, teaNavigationItems, type TeaView } from "./navigation";
import { ActionButton, Brand, Busy, ErrorNotice } from "./shared";
import { Sources } from "./sources";
import { clearStudioDrafts } from "./studio";
import { clearTaskDrafts } from "./task-panel";
import { UpgradeAccount } from "./upgrade-account";
import { Workspace, clearChatDrafts } from "./workspace";

function Welcome({
  onAuthenticated,
  initialError,
}: {
  onAuthenticated: () => void;
  initialError: string;
}) {
  const [auth, setAuth] = useState<"login" | "register" | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState(initialError);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const pending = useRef(false);
  async function login(mode: "demo" | "login" | "register") {
    if (pending.current) return;
    pending.current = true;
    setBusy(mode);
    setError("");
    try {
      await post<User>(
        `/auth/${mode}`,
        mode === "demo"
          ? {}
          : { email, password, ...(mode === "register" ? { name } : {}) },
      );
      onAuthenticated();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      pending.current = false;
      setBusy("");
    }
  }
  return (
    <div className="tea-welcome">
      <header>
        <Brand />
        <span className="tea-welcome-tag">
          中国 → 印度尼西亚 <span>DEMO</span>
        </span>
      </header>
      <main>
        <div className="tea-welcome-copy">
          <h1>茶叶出口工作台</h1>
          <p>集中管理出口业务、材料与办理进度。</p>
          <div className="tea-welcome-actions">
            <ActionButton
              size="lg"
              busy={busy === "demo"}
              disabled={!!busy}
              onClick={() => login("demo")}
            >
              进入演示工作台
              <ArrowRight size={17} />
            </ActionButton>
            <Button
              variant="ghost"
              disabled={!!busy}
              onClick={() => setAuth("login")}
            >
              登录我的账号
              <ChevronRight size={16} />
            </Button>
          </div>
          <div className="tea-welcome-note">
            <ShieldCheck size={14} />
            演示业务独立保存 · 示例流程可配置
          </div>
          {error && <ErrorNotice message={error} />}
        </div>
      </main>
      <footer>茶渡 · 出口业务办理空间</footer>
      <Dialog
        open={!!auth}
        onOpenChange={(open) => {
          if (!open) setAuth(null);
        }}
      >
        <DialogContent className="tea-theme tea-dialog">
          <DialogHeader>
            <DialogTitle>
              {auth === "register" ? "创建你的茶渡账号" : "欢迎回到茶渡"}
            </DialogTitle>
            <DialogDescription>
              业务、材料与办理记录保存在你的账号中。
            </DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (auth) void login(auth);
            }}
          >
            {auth === "register" && (
              <label className="tea-field">
                <span>姓名</span>
                <Input
                  autoComplete="name"
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
            )}
            <label className="tea-field">
              <span>邮箱</span>
              <Input
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label className="tea-field">
              <span>密码 · 至少 10 个字符</span>
              <Input
                type="password"
                minLength={10}
                autoComplete={
                  auth === "register" ? "new-password" : "current-password"
                }
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            {error && <ErrorNotice message={error} />}
            <ActionButton type="submit" busy={!!busy} className="tea-full">
              {auth === "register" ? "创建账号" : "登录"}
            </ActionButton>
            <button
              type="button"
              className="tea-auth-switch"
              onClick={() => {
                setAuth(auth === "login" ? "register" : "login");
                setError("");
              }}
            >
              {auth === "login" ? "还没有账号？创建一个" : "已有账号？返回登录"}
            </button>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function NewCase({
  open,
  onOpenChange,
  templates,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  templates: Template[];
  onCreated: (record: CaseDetail) => void;
}) {
  const [templateId, setTemplateId] = useState(templates[0]?.id ?? "");
  const template = templates.find((t) => t.id === templateId);
  const [mode, setMode] = useState(
    template?.modes.find((m) => m.enabled)?.id ?? "",
  );
  const [title, setTitle] = useState("");
  const [company, setCompany] = useState("");
  const [product, setProduct] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  async function create() {
    if (pending.current || !template) return;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const record = await post<CaseDetail>("/cases", {
        title,
        company,
        product,
        mode_id: mode,
        template_id: template.id,
      });
      onCreated(record);
      onOpenChange(false);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={busy ? undefined : onOpenChange}>
      <DialogContent className="tea-theme tea-dialog">
        <DialogHeader>
          <DialogTitle>开启一段新的出口旅程</DialogTitle>
          <DialogDescription>
            先告诉我们这次业务的基本信息，后续资料可以逐步补充。
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <label className="tea-field">
            <span>业务名称</span>
            <Input
              required
              maxLength={120}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="例如：秋季乌龙茶出口准备"
            />
          </label>
          <div className="tea-form-row">
            <label className="tea-field">
              <span>企业名称</span>
              <Input
                required
                value={company}
                onChange={(e) => setCompany(e.target.value)}
                placeholder="你的企业"
              />
            </label>
            <label className="tea-field">
              <span>茶叶产品</span>
              <Input
                required
                value={product}
                onChange={(e) => setProduct(e.target.value)}
                placeholder="例如：乌龙茶"
              />
            </label>
          </div>
          <label className="tea-field">
            <span>业务模板</span>
            <select
              value={templateId}
              onChange={(e) => {
                setTemplateId(e.target.value);
                setMode(
                  templates
                    .find((t) => t.id === e.target.value)
                    ?.modes.find((m) => m.enabled)?.id ?? "",
                );
              }}
            >
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.title} · v{t.version}
                </option>
              ))}
            </select>
          </label>
          {template && (
            <>
              <div className="tea-route-preview">
                <span>{template.origin}</span>
                <ArrowRight size={17} />
                <span>{template.destination}</span>
              </div>
              <label className="tea-field">
                <span>办理模式</span>
                <select
                  required
                  value={mode}
                  onChange={(e) => setMode(e.target.value)}
                >
                  {template.modes
                    .filter((m) => m.enabled)
                    .map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name}
                      </option>
                    ))}
                </select>
              </label>
              <p className="tea-small-note">
                {template.modes.find((m) => m.id === mode)?.description}
              </p>
              <div className="tea-note">
                将创建 {template.stages.length} 个主体节点、
                {template.stages.reduce((n, s) => n + s.tasks.length, 0)}{" "}
                项任务，并绑定当前模板版本。
              </div>
            </>
          )}
          {error && <ErrorNotice message={error} />}
          <ActionButton
            className="tea-full"
            type="submit"
            busy={busy}
            disabled={!mode || !template}
          >
            创建业务并开始办理
            <ArrowRight size={15} />
          </ActionButton>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function TeaApp({
  initialAdminPage,
  requestedStage,
}: { initialAdminPage?: AdminPage; requestedStage?: string } = {}) {
  const router = useRouter();
  const [bootstrap, setBootstrap] = useState<Bootstrap>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [sessionKey, setSessionKey] = useState(0);
  const [view, setView] = useState<TeaView>(
    initialAdminPage ? "studio" : "workspace",
  );
  const [caseId, setCaseId] = useState("");
  const [record, setRecord] = useState<CaseDetail>();
  const [recordLoading, setRecordLoading] = useState(false);
  const [recordError, setRecordError] = useState("");
  const [recordReload, setRecordReload] = useState(0);
  const [stageId, setStageId] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadScope, setUploadScope] = useState<string>();
  const [libraryRevision, setLibraryRevision] = useState(0);
  const [logoutBusy, setLogoutBusy] = useState(false);
  useEffect(() => {
    const expired = () => {
      setBootstrap(undefined);
      setError("登录已过期，请重新登录。未保存的任务与文稿草稿已保留。");
    };
    window.addEventListener("tea:session-expired", expired);
    return () => window.removeEventListener("tea:session-expired", expired);
  }, []);
  useEffect(() => {
    document.documentElement.lang = "zh-CN";
    const abort = new AbortController();
    setLoading(true);
    teaRequest<Bootstrap>("/bootstrap", { signal: abort.signal })
      .then((data) => {
        setBootstrap(data);
        setError("");
        setCaseId((current) =>
          data.cases.some((c) => c.id === current)
            ? current
            : (data.cases[0]?.id ?? ""),
        );
      })
      .catch((e) => {
        if (abort.signal.aborted) return;
        setBootstrap(undefined);
        if (!(e instanceof TeaApiError && e.status === 401))
          setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [sessionKey]);
  const userId = bootstrap?.user.id;
  useEffect(() => {
    if (!caseId || !userId) {
      setRecord(undefined);
      return;
    }
    const abort = new AbortController();
    setRecordLoading(true);
    setRecordError("");
    teaRequest<CaseDetail>(`/cases/${caseId}`, { signal: abort.signal })
      .then((data) => {
        setRecord(data);
        setStageId((current) =>
          data.stages.some((s) => s.id === current)
            ? current
            : data.current_stage_id,
        );
      })
      .catch((e) => {
        if (!abort.signal.aborted) setRecordError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setRecordLoading(false);
      });
    return () => abort.abort();
  }, [caseId, recordReload, userId]);
  const updateRecord = useCallback((next: CaseDetail) => {
    setRecord((current) =>
      current?.id === next.id && next.revision >= current.revision
        ? next
        : current,
    );
    setBootstrap((current) =>
      current
        ? {
            ...current,
            cases: current.cases.map((c) => (c.id === next.id ? next : c)),
          }
        : current,
    );
  }, []);
  function selectCase(id: string) {
    if (id !== caseId) {
      setRecord(undefined);
      setStageId("");
    }
    setCaseId(id);
    setView("workspace");
  }
  async function logout() {
    if (logoutBusy) return;
    setLogoutBusy(true);
    try {
      await post("/auth/logout");
      clearArtifactDrafts();
      clearTaskDrafts();
      clearChatDrafts();
      clearStudioDrafts();
      setBootstrap(undefined);
      setRecord(undefined);
      setCaseId("");
      setError("");
      setView("workspace");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLogoutBusy(false);
    }
  }
  if (loading)
    return (
      <div className="tea-theme tea-loading-screen">
        <Brand />
        <Busy label="正在打开你的工作台…" />
      </div>
    );
  if (!bootstrap)
    return (
      <div className="tea-theme">
        <Welcome
          onAuthenticated={() => setSessionKey((k) => k + 1)}
          initialError={error}
        />
      </div>
    );
  if (initialAdminPage || view === "studio")
    return (
      <Admin
        key={bootstrap.user.id}
        bootstrap={bootstrap}
        page={initialAdminPage ?? "workflow"}
        requestedStage={requestedStage}
        onLogout={logout}
        logoutBusy={logoutBusy}
        onPublished={(template) =>
          setBootstrap((current) =>
            current
              ? {
                  ...current,
                  templates: current.templates.some(
                    (item) => item.id === template.id,
                  )
                    ? current.templates.map((item) =>
                        item.id === template.id ? template : item,
                      )
                    : [...current.templates, template],
                }
              : current,
          )
        }
      />
    );
  return (
    <SidebarProvider className="tea-theme tea-app">
      <TeaNavigation
        view={view}
        user={bootstrap.user}
        cases={bootstrap.cases}
        caseId={caseId}
        record={record}
        stageId={stageId}
        onNavigate={(next) =>
          next === "studio" ? router.push("/tea/admin/workflow") : setView(next)
        }
        onCase={selectCase}
        onStage={(id) => {
          setStageId(id);
          setView("workspace");
        }}
        onCreate={() => setCreateOpen(true)}
      />
      <SidebarInset className="tea-main min-w-0">
        {(view !== "workspace" || !record || recordLoading || recordError) && (
          <header className="tea-topbar">
            <SidebarTrigger className="md:hidden" aria-label="展开导航" />
            <div className="tea-breadcrumb">
              {view === "workspace" ? (
                <>
                  <span>出口业务</span>
                  <ChevronRight size={13} />
                  <strong>{record?.title ?? "开始新业务"}</strong>
                </>
              ) : (
                <strong>
                  {view === "account"
                    ? "账号管理"
                    : teaNavigationItems.find((n) => n.id === view)?.label}
                </strong>
              )}
            </div>
          </header>
        )}
        {error && <ErrorNotice message={error} />}
        {view === "workspace" ? (
          recordLoading ? (
            <Busy label="正在读取业务与办理进度…" />
          ) : recordError ? (
            <div className="tea-page">
              <ErrorNotice
                message={recordError}
                onRetry={() => setRecordReload((r) => r + 1)}
              />
            </div>
          ) : record ? (
            <Workspace
              key={record.id}
              record={record}
              stageId={stageId}
              setStageId={setStageId}
              capabilities={bootstrap.capabilities}
              onUpdate={updateRecord}
              onUpload={() => setUploadOpen(true)}
            />
          ) : (
            <div className="tea-no-cases">
              <Sprout size={40} strokeWidth={1.3} />
              <h1>还没有出口业务</h1>
              <p>创建一次出口业务，把资料、对话和进度放在一起。</p>
              <Button onClick={() => setCreateOpen(true)}>
                <Plus size={16} />
                新建出口业务
              </Button>
            </div>
          )
        ) : view === "library" ? (
          <Library
            revision={libraryRevision}
            cases={bootstrap.cases}
            initialCaseId={caseId}
            onUpload={(scope) => {
              setUploadScope(scope);
              setUploadOpen(true);
            }}
          />
        ) : view === "sources" ? (
          <Sources />
        ) : (
          <div className="tea-page tea-account-page">
            <h1>账号管理</h1>
            <p>
              {bootstrap.user.is_demo
                ? "你正在使用独立的演示账号。"
                : bootstrap.user.email}
            </p>
            <div className="tea-account-summary">
              <UserRound size={23} />
              <div>
                <strong>{bootstrap.user.name}</strong>
                <span>
                  {bootstrap.user.is_demo
                    ? "同一浏览器可继续办理，退出前可保存文稿"
                    : "资料与业务按账号独立管理"}
                </span>
              </div>
              <span>{bootstrap.cases.length} 次业务</span>
            </div>
            {bootstrap.user.is_demo && (
              <UpgradeAccount
                user={bootstrap.user}
                onUpdated={() => setSessionKey((key) => key + 1)}
              />
            )}
            <ActionButton variant="outline" busy={logoutBusy} onClick={logout}>
              <LogOut size={15} />
              退出登录
            </ActionButton>
          </div>
        )}
      </SidebarInset>
      <NewCase
        key={createOpen ? "new-open" : "new-closed"}
        open={createOpen}
        onOpenChange={setCreateOpen}
        templates={bootstrap.templates}
        onCreated={(next) => {
          setBootstrap((b) => (b ? { ...b, cases: [next, ...b.cases] } : b));
          setRecord(next);
          setCaseId(next.id);
          setStageId(next.current_stage_id);
          setView("workspace");
        }}
      />
      <UploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        caseId={view === "workspace" ? caseId : uploadScope}
        cases={bootstrap.cases}
        onDone={() => {
          setLibraryRevision((r) => r + 1);
          setRecordReload((r) => r + 1);
        }}
      />
    </SidebarProvider>
  );
}
