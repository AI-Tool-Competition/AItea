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
import { clearCollectionDrafts } from "@/core/tea/collection-drafts";
import {
  type Bootstrap,
  type CaseDetail,
  type Document,
  type ListResult,
  type NodeConversation,
  type Template,
  type User,
} from "@/core/tea/types";
import { clearLegalDrafts } from "@/core/tea/use-legal-workbench";

import { Admin, type AdminPage } from "./admin";
import { AdminBootstrap } from "./admin-bootstrap";
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
  adminMode = false,
  onBack,
}: {
  onAuthenticated: () => void;
  initialError: string;
  adminMode?: boolean;
  onBack?: () => void;
}) {
  const [auth, setAuth] = useState<"login" | "register" | null>(
    adminMode ? "login" : null,
  );
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
      const user = await post<User>(
        `/auth/${mode}`,
        mode === "demo"
          ? {}
          : {
              email,
              password,
              ...(mode === "register" ? { name } : {}),
            },
      );
      if (mode === "register" && !user.can_login) {
        setError(
          "账号已创建，等待管理员开放登录。获准后可用此邮箱和密码登录。",
        );
        return;
      }
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
          中国 → 印尼 / 新加坡 <span>六堡茶</span>
        </span>
      </header>
      <main>
        <div className="tea-welcome-copy">
          <h1>{adminMode ? "管理后台" : "茶叶出口工作台"}</h1>
          {!adminMode && <p>集中管理出口业务、材料与办理进度。</p>}
          {!adminMode ? (
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
          ) : (
            <p>请使用管理员账号登录。初始账号：admin，密码：admin123。</p>
          )}
          {!adminMode && (
            <div className="tea-welcome-note">
              <ShieldCheck size={14} />
              体验业务独立保存 · 四条六堡茶出口流程
            </div>
          )}
          {error && <ErrorNotice message={error} />}
        </div>
      </main>
      <footer>茶渡 · 出口业务办理空间</footer>
      <Dialog
        open={adminMode || !!auth}
        onOpenChange={(open) => {
          if (!open) {
            if (adminMode) onBack?.();
            else setAuth(null);
          }
        }}
      >
        <DialogContent className="tea-theme tea-dialog">
          <DialogHeader>
            <DialogTitle>
              {adminMode
                ? "管理员登录"
                : auth === "register"
                  ? "创建你的茶渡账号"
                  : "欢迎回到茶渡"}
            </DialogTitle>
            <DialogDescription>
              {adminMode
                ? "登录后进入茶渡管理后台。"
                : "业务、材料与办理记录保存在你的账号中。"}
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
              <span>{auth === "register" ? "邮箱" : "邮箱或管理员账号"}</span>
              <Input
                type={auth === "register" ? "email" : "text"}
                autoComplete={auth === "register" ? "email" : "username"}
                placeholder={auth === "login" ? "邮箱或 admin" : undefined}
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label className="tea-field">
              <span>
                {auth === "register" ? "密码 · 至少 10 个字符" : "密码"}
              </span>
              <Input
                type="password"
                minLength={auth === "register" ? 10 : 1}
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
              {adminMode
                ? "登录管理后台"
                : auth === "register"
                  ? "创建账号"
                  : "登录"}
            </ActionButton>
            {adminMode && onBack && (
              <button
                type="button"
                className="tea-auth-switch"
                onClick={onBack}
              >
                返回工作台
              </button>
            )}
            {!adminMode && (
              <button
                type="button"
                className="tea-auth-switch"
                onClick={() => {
                  setAuth(auth === "login" ? "register" : "login");
                  setError("");
                }}
              >
                {auth === "login"
                  ? "还没有账号？创建一个"
                  : "已有账号？返回登录"}
              </button>
            )}
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
  const [templateId, setTemplateId] = useState(
    templates.find((item) => !item.is_demo)?.id ?? templates[0]?.id ?? "",
  );
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
              placeholder="例如：六堡茶 · 新加坡线上出口"
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
                placeholder="例如：六堡茶"
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
  const [conversationId, setConversationId] = useState("guide");
  const [conversationData, setConversationData] = useState<{
    scope: string;
    items: NodeConversation[];
  }>({ scope: "", items: [] });
  const [conversationBusy, setConversationBusy] = useState(false);
  const [conversationError, setConversationError] = useState("");
  const [conversationReload, setConversationReload] = useState(0);
  const creatingConversation = useRef<AbortController | undefined>(undefined);
  const conversationScope = `${bootstrap?.user.id ?? ""}:${caseId}`;
  const currentConversationScope = useRef("");
  currentConversationScope.current = `${conversationScope}:${stageId}`;
  const [createOpen, setCreateOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadScope, setUploadScope] = useState<string>();
  const [libraryRevision, setLibraryRevision] = useState(0);
  const [uploadedDocument, setUploadedDocument] = useState<{
    ownerId: string;
    document: Document;
  }>();
  const [libraryCaseId, setLibraryCaseId] = useState("");
  const [logoutBusy, setLogoutBusy] = useState(false);
  useEffect(() => {
    const expired = () => {
      clearChatDrafts();
      clearLegalDrafts();
      clearCollectionDrafts();
      setBootstrap(undefined);
      setConversationId("guide");
      setConversationError("");
      setUploadedDocument(undefined);
      setLibraryCaseId("");
      setView("workspace");
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
        if (abort.signal.aborted) return;
        setBootstrap(data);
        setError("");
        setCaseId((current) =>
          data.cases.some((c) => c.id === current)
            ? current
            : (data.cases[0]?.id ?? ""),
        );
        setLibraryCaseId((current) =>
          data.cases.some((business) => business.id === current) ? current : "",
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
    if (!userId || !caseId || !stageId || record?.id !== caseId) return;
    const abort = new AbortController();
    const read = async () => {
      const items: NodeConversation[] = [];
      let total = 1;
      while (items.length < total && !abort.signal.aborted) {
        const result = await teaRequest<ListResult<NodeConversation>>(
          `/cases/${encodeURIComponent(caseId)}/conversations?limit=200&offset=${items.length}`,
          { signal: abort.signal },
        );
        items.push(...result.items);
        total = result.total;
        if (!result.items.length) break;
      }
      if (!abort.signal.aborted) {
        setConversationData((previous) => ({
          scope: conversationScope,
          items: [
            ...new Map(
              [
                ...(previous.scope === conversationScope ? previous.items : []),
                ...items,
              ].map((item) => [item.id, item]),
            ).values(),
          ],
        }));
        setConversationError("");
      }
    };
    void read().catch((reason) => {
      if (!abort.signal.aborted) setConversationError(errorMessage(reason));
    });
    return () => abort.abort();
  }, [
    caseId,
    userId,
    stageId,
    record?.id,
    record?.updated_at,
    conversationScope,
    conversationReload,
  ]);
  useEffect(() => {
    setConversationBusy(false);
    return () => {
      creatingConversation.current?.abort();
      creatingConversation.current = undefined;
    };
  }, [conversationScope]);
  async function createConversation(targetStage: string) {
    if (!record || creatingConversation.current) return;
    const abort = new AbortController();
    creatingConversation.current = abort;
    const target = `${conversationScope}:${targetStage}`;
    setStageId(targetStage);
    setConversationBusy(true);
    try {
      const conversation = await post<NodeConversation>(
        `/cases/${encodeURIComponent(record.id)}/stages/${encodeURIComponent(targetStage)}/conversations`,
        { client_request_id: crypto.randomUUID() },
        abort.signal,
      );
      if (abort.signal.aborted || currentConversationScope.current !== target)
        return;
      setConversationData((previous) => ({
        scope: conversationScope,
        items: [
          ...(previous.scope === conversationScope ? previous.items : []),
          conversation,
        ],
      }));
      setConversationId(conversation.id);
      setView("workspace");
    } catch (reason) {
      if (!abort.signal.aborted && currentConversationScope.current === target)
        setError(errorMessage(reason));
    } finally {
      if (creatingConversation.current === abort) {
        creatingConversation.current = undefined;
        setConversationBusy(false);
      }
    }
  }
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
        if (abort.signal.aborted) return;
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
    setUploadedDocument(undefined);
    setConversationId("guide");
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
      clearLegalDrafts();
      clearStudioDrafts();
      clearCollectionDrafts();
      setBootstrap(undefined);
      setRecord(undefined);
      setCaseId("");
      setUploadedDocument(undefined);
      setConversationId("guide");
      setConversationError("");
      setLibraryCaseId("");
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
        <Busy
          label={initialAdminPage ? "正在打开管理后台…" : "正在打开你的工作台…"}
        />
      </div>
    );
  if (!bootstrap)
    return (
      <div className="tea-theme">
        <Welcome
          onAuthenticated={() => setSessionKey((k) => k + 1)}
          initialError={error}
          adminMode={!!initialAdminPage}
          onBack={initialAdminPage ? () => router.replace("/tea") : undefined}
        />
      </div>
    );
  if (
    initialAdminPage &&
    !bootstrap.user.is_admin &&
    !bootstrap.user.is_demo &&
    bootstrap.admin_setup_required
  )
    return (
      <div className="tea-theme tea-admin-denied">
        <Brand />
        <h1>首次设置管理后台</h1>
        <p>请用管理员引导码将当前账号设为首位管理员。</p>
        <AdminBootstrap onReady={() => setSessionKey((key) => key + 1)} />
        <Button variant="ghost" onClick={() => router.replace("/tea")}>
          返回工作台
        </Button>
      </div>
    );
  if (initialAdminPage && !bootstrap.user.is_admin)
    return (
      <div className="tea-theme tea-admin-denied">
        <Brand />
        <h1>仅管理员可进入管理后台</h1>
        <p>当前账号没有管理员权限。可以切换到管理员账号，或返回工作台。</p>
        <Button disabled={logoutBusy} onClick={logout}>
          {logoutBusy ? "正在退出…" : "切换账号登录"}
        </Button>
        <Button variant="ghost" onClick={() => router.replace("/tea")}>
          返回工作台
        </Button>
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
        key={`${bootstrap.user.id}:${caseId}`}
        view={view}
        user={bootstrap.user}
        cases={bootstrap.cases}
        caseId={caseId}
        record={record}
        recordLoading={recordLoading}
        stageId={stageId}
        conversations={
          conversationData.scope === conversationScope
            ? conversationData.items
            : []
        }
        conversationId={conversationId}
        conversationBusy={conversationBusy}
        onConversation={(nodeId, id) => {
          setStageId(nodeId);
          setConversationId(id);
          setView("workspace");
        }}
        onCreateConversation={(id) => void createConversation(id)}
        libraryCaseId={libraryCaseId}
        onNavigate={(next) => {
          setUploadedDocument(undefined);
          if (next === "studio") router.push("/tea/admin/workflow");
          else {
            if (next === "library") setLibraryCaseId("");
            setView(next);
          }
        }}
        onCase={selectCase}
        onStage={(id) => {
          setUploadedDocument(undefined);
          setConversationId("guide");
          setStageId(id);
          setView("workspace");
        }}
        onLibrary={(id) => {
          setUploadedDocument(undefined);
          selectCase(id);
          setLibraryCaseId(id);
          setView("library");
        }}
        onCreate={() => setCreateOpen(true)}
      />
      <SidebarInset className="tea-main min-w-0">
        {(view !== "workspace" || !record || recordLoading || recordError) && (
          <header className="tea-topbar">
            <SidebarTrigger className="md:hidden" aria-label="展开导航" />
            <div className="tea-breadcrumb">
              {view === "workspace" || (view === "library" && libraryCaseId) ? (
                <>
                  <span>出口业务</span>
                  <ChevronRight size={13} />
                  <strong>
                    {view === "library"
                      ? `${bootstrap.cases.find((business) => business.id === libraryCaseId)?.title ?? "当前业务"} · 资料库`
                      : (record?.title ?? "开始新业务")}
                  </strong>
                </>
              ) : (
                <strong>
                  {view === "account"
                    ? "账号管理"
                    : view === "library"
                      ? "资料库"
                      : teaNavigationItems.find((n) => n.id === view)?.label}
                </strong>
              )}
            </div>
          </header>
        )}
        {error && <ErrorNotice message={error} />}
        {view === "workspace" && conversationError && (
          <ErrorNotice
            message={conversationError}
            onRetry={() => setConversationReload((value) => value + 1)}
          />
        )}
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
              key={`${bootstrap.user.id}:${record.id}`}
              accountId={bootstrap.user.id}
              record={record}
              stageId={stageId}
              setStageId={(id) => {
                if (id !== stageId) setConversationId("guide");
                setStageId(id);
              }}
              conversationId={conversationId}
              onConversation={(id) => setConversationId(id)}
              capabilities={bootstrap.capabilities}
              uploadedDocument={
                uploadedDocument?.ownerId === bootstrap.user.id
                  ? uploadedDocument.document
                  : undefined
              }
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
            key={`${bootstrap.user.id}:${libraryCaseId || "public"}`}
            ownerId={bootstrap.user.id}
            revision={libraryRevision}
            cases={bootstrap.cases}
            initialCaseId={libraryCaseId}
            uploadedDocument={
              uploadedDocument?.ownerId === bootstrap.user.id
                ? uploadedDocument.document
                : undefined
            }
            scopeLocked
            onUpload={(scope) => {
              setUploadScope(scope);
              setUploadOpen(true);
            }}
          />
        ) : view === "sources" ? (
          <Sources key={bootstrap.user.id} ownerId={bootstrap.user.id} />
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
                onUpdated={(message) => {
                  if (message) setError(message);
                  setSessionKey((key) => key + 1);
                }}
              />
            )}
            {"is_admin" in bootstrap.user &&
              bootstrap.user.is_admin === true && (
                <Button
                  variant="outline"
                  onClick={() => router.push("/tea/admin/workflow")}
                >
                  <ShieldCheck size={15} />
                  管理后台
                </Button>
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
          setConversationId("guide");
          setStageId(next.current_stage_id);
          setView("workspace");
        }}
      />
      <UploadDialog
        key={bootstrap.user.id}
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        caseId={view === "workspace" ? caseId : uploadScope}
        cases={bootstrap.cases}
        onDone={(document) => {
          setUploadedDocument({ ownerId: bootstrap.user.id, document });
          if (view === "library") setLibraryCaseId(document.case_id ?? "");
          else if (view === "workspace" && document.case_id !== caseId) {
            setLibraryCaseId(document.case_id ?? "");
            setView("library");
          }
          setLibraryRevision((r) => r + 1);
          setRecordReload((r) => r + 1);
        }}
      />
    </SidebarProvider>
  );
}
