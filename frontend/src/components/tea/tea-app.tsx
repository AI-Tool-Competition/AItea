"use client";
import {
  ArrowRight,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  FolderClosed,
  FolderOpen,
  Globe2,
  Leaf,
  LockKeyhole,
  LogOut,
  Menu,
  MessageSquare,
  Plus,
  Settings2,
  ShieldCheck,
  Sprout,
  Upload,
  UserRound,
} from "lucide-react";
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
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { errorMessage, post, TeaApiError, teaRequest } from "@/core/tea/api";
import {
  type Bootstrap,
  type CaseDetail,
  type Template,
  type User,
} from "@/core/tea/types";

import { clearArtifactDrafts } from "./artifact-panel";
import { Library, UploadDialog } from "./library";
import { ActionButton, Brand, Busy, ErrorNotice } from "./shared";
import { Sources } from "./sources";
import { Studio, clearStudioDrafts } from "./studio";
import { clearTaskDrafts } from "./task-panel";
import { UpgradeAccount } from "./upgrade-account";
import { Workspace, clearChatDrafts } from "./workspace";
type View = "workspace" | "library" | "studio" | "sources" | "account";

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
          <span className="tea-eyebrow">
            <span className="tea-live-dot" /> YOUR NEXT CHAPTER, OVERSEAS
          </span>
          <h1>
            好茶的下一站，
            <br />
            <em>是更远的世界。</em>
          </h1>
          <p>
            从一份资料，到一条清晰的出口路径。
            <br />
            茶渡陪你理清手续、找到依据，把每一步稳稳做好。
          </p>
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
        <div className="tea-welcome-art" aria-hidden="true">
          <div className="tea-orbit orbit-one" />
          <div className="tea-orbit orbit-two" />
          <div className="tea-art-label label-origin">
            CHINA<small>始于一片好茶</small>
          </div>
          <div className="tea-art-leaf">
            <Leaf strokeWidth={0.7} />
            <span>茶</span>
          </div>
          <div className="tea-art-label label-destination">
            INDONESIA<small>抵达新的可能</small>
          </div>
          <div className="tea-art-coordinate">
            30°16′ N &nbsp; / &nbsp; 106°49′ E
          </div>
        </div>
      </main>
      <footer>
        <div>
          <span>01</span>
          <strong>流程清晰</strong>
          <p>面向主体，逐步推进</p>
        </div>
        <div>
          <span>02</span>
          <strong>回答有据</strong>
          <p>引用原文，保留版本</p>
        </div>
        <div>
          <span>03</span>
          <strong>成果可用</strong>
          <p>协同起草，逐项核验</p>
        </div>
        <small>茶渡 · TEA BEYOND BORDERS</small>
      </footer>
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

export function TeaApp() {
  const [bootstrap, setBootstrap] = useState<Bootstrap>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [sessionKey, setSessionKey] = useState(0);
  const [view, setView] = useState<View>("workspace");
  const [caseId, setCaseId] = useState("");
  const [record, setRecord] = useState<CaseDetail>();
  const [recordLoading, setRecordLoading] = useState(false);
  const [recordError, setRecordError] = useState("");
  const [recordReload, setRecordReload] = useState(0);
  const [stageId, setStageId] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
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
    setSidebarOpen(false);
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
  const nav = [
    { id: "workspace", icon: MessageSquare, label: "出口业务" },
    { id: "library", icon: BookOpen, label: "资料库" },
    { id: "studio", icon: Settings2, label: "法务工作台" },
    { id: "sources", icon: Globe2, label: "来源管理" },
  ] as const;
  const businessList = (
    <>
      <div className="tea-sidebar-heading">
        <span>我的出口业务</span>
        <button aria-label="新建出口业务" onClick={() => setCreateOpen(true)}>
          <Plus size={17} />
        </button>
      </div>
      <Button
        variant="outline"
        className="tea-new-case"
        onClick={() => setCreateOpen(true)}
      >
        <Plus size={15} />
        新建出口业务
      </Button>
      <div className="tea-businesses">
        {bootstrap.cases.map((c) => (
          <div className="tea-business" key={c.id}>
            <button
              className={`tea-business-title ${caseId === c.id && view === "workspace" ? "active" : ""}`}
              onClick={() => selectCase(c.id)}
            >
              {caseId === c.id ? (
                <FolderOpen size={17} />
              ) : (
                <FolderClosed size={17} />
              )}
              <span>{c.title}</span>
              {caseId === c.id ? (
                <ChevronDown size={13} />
              ) : (
                <ChevronRight size={13} />
              )}
            </button>
            {caseId === c.id && record?.id === c.id && (
              <div className="tea-subconversations">
                {record.stages.map((stage) => (
                  <button
                    key={stage.id}
                    className={
                      stageId === stage.id && view === "workspace"
                        ? "active"
                        : ""
                    }
                    onClick={() => {
                      setStageId(stage.id);
                      setView("workspace");
                      setSidebarOpen(false);
                    }}
                  >
                    <span className={`tea-tree-dot ${stage.status}`} />
                    <span>{stage.name}</span>
                    {stage.status === "locked" && <LockKeyhole size={11} />}
                    {stage.status === "completed" && <Check size={12} />}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="tea-sidebar-bottom">
        <div className="tea-route-small">
          <span>CN</span>
          <div>
            <small>当前演示路线</small>
            <strong>
              中国 <ArrowRight size={12} /> 印度尼西亚
            </strong>
          </div>
          <span>ID</span>
        </div>
        <p>
          <ShieldCheck size={13} />
          每一步，都有依据可循
        </p>
      </div>
    </>
  );
  return (
    <div className="tea-theme tea-app">
      <aside className="tea-rail">
        <button
          className="tea-logo-button"
          onClick={() => setView("workspace")}
          aria-label="茶渡工作台"
        >
          <Brand compact />
        </button>
        <nav>
          {nav.map((item) => (
            <button
              key={item.id}
              className={view === item.id ? "active" : ""}
              onClick={() => setView(item.id)}
              aria-label={item.label}
              title={item.label}
            >
              <item.icon size={21} strokeWidth={1.7} />
              <span>{item.label}</span>
            </button>
          ))}
          <button
            onClick={() => setUploadOpen(true)}
            aria-label="上传资料"
            title="上传资料"
          >
            <Upload size={21} strokeWidth={1.7} />
            <span>上传</span>
          </button>
        </nav>
        <button
          className={`tea-account-button ${view === "account" ? "active" : ""}`}
          onClick={() => setView("account")}
          aria-label="账号管理"
          title="账号管理"
        >
          {bootstrap.user.name.slice(0, 1)}
        </button>
      </aside>
      <aside className="tea-sidebar">
        <div className="tea-sidebar-brand">
          茶渡<span>出口工作台</span>
        </div>
        {businessList}
      </aside>
      <div className="tea-main">
        <header className="tea-topbar">
          <button
            className="tea-mobile-menu"
            aria-label="展开业务目录"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu size={20} />
          </button>
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
                  : nav.find((n) => n.id === view)?.label}
              </strong>
            )}
          </div>
          <div className="tea-topbar-meta">
            <span className="tea-mode-indicator">
              <span />
              {bootstrap.capabilities.assistant_mode === "demo"
                ? "演示助手"
                : "AI 助手"}
            </span>
            <span className="tea-topbar-divider" />
            <span>{bootstrap.user.name}</span>
          </div>
        </header>
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
              <h1>从这里，走向新的市场。</h1>
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
        ) : view === "studio" ? (
          <Studio
            ownerId={bootstrap.user.id}
            templates={bootstrap.templates}
            onPublished={(template) =>
              setBootstrap((b) =>
                b
                  ? {
                      ...b,
                      templates: b.templates.some((t) => t.id === template.id)
                        ? b.templates.map((t) =>
                            t.id === template.id ? template : t,
                          )
                        : [...b.templates, template],
                    }
                  : b,
              )
            }
          />
        ) : view === "sources" ? (
          <Sources />
        ) : (
          <div className="tea-page tea-account-page">
            <span className="tea-eyebrow">YOUR WORKSPACE</span>
            <h1>你好，{bootstrap.user.name}。</h1>
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
      </div>
      <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
        <SheetContent side="left" className="tea-theme tea-mobile-sidebar">
          <SheetHeader>
            <SheetTitle>茶渡 · 业务目录</SheetTitle>
            <SheetDescription>一次业务，一个完整的办理空间。</SheetDescription>
          </SheetHeader>
          {businessList}
        </SheetContent>
      </Sheet>
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
    </div>
  );
}
