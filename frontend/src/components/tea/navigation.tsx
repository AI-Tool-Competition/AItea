"use client";

import {
  Check,
  ChevronDown,
  ChevronRight,
  Ellipsis,
  FolderClosed,
  FolderOpen,
  LockKeyhole,
  LoaderCircle,
  MessageSquare,
  Plus,
  Settings2,
  UserRound,
  BookOpen,
  Globe2,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import type {
  CaseDetail,
  CaseSummary,
  NodeConversation,
  User,
} from "@/core/tea/types";

import { Brand } from "./shared";

const RECENT_CHAT_COUNT = 4;

export type TeaView =
  | "workspace"
  | "library"
  | "studio"
  | "sources"
  | "account";
export const teaNavigationItems = [
  { id: "workspace", icon: MessageSquare, label: "出口业务" },
  { id: "library", icon: BookOpen, label: "资料库" },
  { id: "studio", icon: Settings2, label: "管理后台" },
  { id: "sources", icon: Globe2, label: "来源管理" },
] as const;

type Props = {
  view: TeaView;
  user: User;
  cases: CaseSummary[];
  caseId: string;
  record?: CaseDetail;
  recordLoading: boolean;
  stageId: string;
  conversations?: NodeConversation[];
  conversationId?: string;
  conversationBusy?: boolean;
  onNavigate: (view: TeaView) => void;
  onCase: (id: string) => void;
  onStage: (id: string) => void;
  onCreate: () => void;
  onConversation?: (stage: string, id: string) => void;
  onCreateConversation?: (stage: string) => void;
};
export function TeaNavigation({
  view,
  user,
  cases,
  caseId,
  record,
  recordLoading,
  stageId,
  onNavigate,
  onCase,
  onStage,
  onCreate,
  conversations = [],
  conversationId = "guide",
  conversationBusy = false,
  onConversation,
  onCreateConversation,
}: Props) {
  const { open, isMobile, setOpenMobile } = useSidebar();
  const [more, setMore] = useState(false);
  const [projectsOpen, setProjectsOpen] = useState(true);
  const [projectExpanded, setProjectExpanded] = useState(true);
  const [allChats, setAllChats] = useState<Set<string>>(() => new Set());
  const currentFolder = useRef<HTMLElement>(null);
  const [expanded, setExpanded] = useState(stageId);
  useEffect(() => {
    setExpanded(stageId);
    setProjectExpanded(true);
  }, [stageId]);
  useEffect(() => {
    currentFolder.current?.scrollIntoView?.({ block: "nearest" });
  }, [stageId, record?.id, open, isMobile]);
  const stages =
    record?.stages.filter((stage) => more || stage.id === stageId) ?? [];
  function navigate(action: () => void) {
    action();
    setOpenMobile(false);
  }
  return (
    <Sidebar variant="sidebar" collapsible="icon" className="tea-navigation">
      <SidebarHeader className="py-0">
        <div className="tea-navigation-header">
          <button
            className="tea-navigation-brand"
            aria-label="茶渡工作台"
            onClick={() => navigate(() => onNavigate("workspace"))}
          >
            <Brand />
          </button>
          <SidebarTrigger aria-label="切换侧栏" />
        </div>
      </SidebarHeader>
      <SidebarContent>
        {open || isMobile ? (
          <Collapsible
            open={projectsOpen}
            onOpenChange={setProjectsOpen}
            className="tea-business-context"
          >
            <div className="tea-business-switch-row">
              <CollapsibleTrigger className="tea-projects-title">
                <span>项目管理</span>
                <ChevronDown size={14} />
              </CollapsibleTrigger>
              <button
                className="tea-business-create"
                aria-label="新建项目"
                title="新建项目"
                onClick={() => navigate(onCreate)}
              >
                <Plus size={17} />
              </button>
            </div>
            <CollapsibleContent>
              {cases.map((business) => {
                const selected = business.id === caseId;
                return (
                  <Collapsible
                    key={business.id}
                    open={selected && projectExpanded}
                    onOpenChange={(value) => {
                      if (selected) setProjectExpanded(value);
                      else if (value) navigate(() => onCase(business.id));
                    }}
                    className="tea-project-folder"
                  >
                    <div className="tea-project-folder-heading">
                      <CollapsibleTrigger
                        className="tea-project-folder-name"
                        aria-label={`项目：${business.title}`}
                        title={business.title}
                      >
                        <span
                          className="tea-project-folder-icon"
                          aria-hidden="true"
                        >
                          <FolderClosed size={16} />
                          <ChevronRight size={15} />
                        </span>
                        <span className="tea-project-folder-title">
                          {business.title}
                        </span>
                      </CollapsibleTrigger>
                      {selected && (
                        <DropdownMenu modal={!isMobile}>
                          <DropdownMenuTrigger asChild>
                            <button
                              className="tea-project-switch"
                              title="项目菜单"
                              aria-label="切换业务"
                            >
                              <Ellipsis size={16} />
                            </button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent
                            align="start"
                            sideOffset={5}
                            className="tea-business-switch-menu"
                          >
                            {cases.map((item) => (
                              <DropdownMenuItem
                                key={item.id}
                                onSelect={() => navigate(() => onCase(item.id))}
                              >
                                <span className="tea-business-option">
                                  {item.title}
                                </span>
                                {item.id === caseId && <Check size={14} />}
                              </DropdownMenuItem>
                            ))}
                            {cases.length > 0 && <DropdownMenuSeparator />}
                            <DropdownMenuItem
                              onSelect={() => navigate(onCreate)}
                            >
                              <Plus size={15} />
                              新建项目
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      )}
                    </div>
                    {selected && (
                      <CollapsibleContent>
                        <SidebarGroup
                          className="tea-node-folders"
                          aria-label="当前业务节点"
                        >
                          {recordLoading && (
                            <p role="status" className="tea-business-loading">
                              正在读取节点…
                            </p>
                          )}
                          {stages.map((stage) => {
                            const current =
                              stage.id === stageId && view === "workspace";
                            const opened = expanded === stage.id;
                            const chats = conversations
                              .filter(
                                (item) =>
                                  item.stage_id === stage.id &&
                                  item.kind !== "guide",
                              )
                              .sort((a, b) =>
                                (b.updated_at ?? "").localeCompare(
                                  a.updated_at ?? "",
                                ),
                              );
                            const showingAll = allChats.has(stage.id);
                            const recent = chats.slice(0, RECENT_CHAT_COUNT);
                            const active = current
                              ? chats.find((item) => item.id === conversationId)
                              : undefined;
                            const visibleChats = showingAll
                              ? chats
                              : active && !recent.includes(active)
                                ? [
                                    ...recent.slice(0, RECENT_CHAT_COUNT - 1),
                                    active,
                                  ]
                                : recent;
                            return (
                              <Collapsible
                                key={stage.id}
                                open={opened}
                                onOpenChange={(value) =>
                                  setExpanded(value ? stage.id : "")
                                }
                                asChild
                              >
                                <section
                                  ref={current ? currentFolder : undefined}
                                  className="tea-node-folder"
                                  data-current={current}
                                >
                                  <div className="tea-node-folder-heading">
                                    <CollapsibleTrigger asChild>
                                      <button
                                        className="tea-node-folder-name"
                                        aria-label={stage.name}
                                        title={stage.name}
                                      >
                                        <ChevronRight
                                          className="tea-node-disclosure"
                                          size={14}
                                          aria-hidden="true"
                                        />
                                        <span className="tea-node-folder-title">
                                          {stage.name}
                                        </span>
                                        <span
                                          className="tea-node-folder-status"
                                          title={
                                            stage.status === "locked"
                                              ? "待解锁"
                                              : stage.status === "completed"
                                                ? "已完成"
                                                : "进行中"
                                          }
                                        >
                                          {stage.status === "locked" ? (
                                            <LockKeyhole size={12} />
                                          ) : stage.status === "completed" ? (
                                            <Check size={12} />
                                          ) : (
                                            <i className="tea-node-active-dot" />
                                          )}
                                        </span>
                                      </button>
                                    </CollapsibleTrigger>
                                    <button
                                      className="tea-node-new-conversation"
                                      aria-label={`在${stage.name}新建会话`}
                                      aria-busy={conversationBusy && current}
                                      title={
                                        stage.status === "locked"
                                          ? "节点解锁后可新建会话"
                                          : conversationBusy && current
                                            ? "正在新建…"
                                            : "新建会话"
                                      }
                                      disabled={
                                        stage.status === "locked" ||
                                        conversationBusy
                                      }
                                      onClick={() => {
                                        setExpanded(stage.id);
                                        navigate(() =>
                                          onCreateConversation?.(stage.id),
                                        );
                                      }}
                                    >
                                      {conversationBusy && current ? (
                                        <LoaderCircle
                                          size={16}
                                          className="animate-spin motion-reduce:animate-none"
                                        />
                                      ) : (
                                        <Plus size={16} />
                                      )}
                                    </button>
                                  </div>
                                  <CollapsibleContent
                                    className="tea-node-entries"
                                    role="group"
                                    aria-label={`${stage.name}的会话`}
                                  >
                                    <button
                                      className={
                                        current && conversationId === "guide"
                                          ? "active"
                                          : ""
                                      }
                                      aria-current={
                                        current && conversationId === "guide"
                                          ? "page"
                                          : undefined
                                      }
                                      onClick={() =>
                                        navigate(() => onStage(stage.id))
                                      }
                                    >
                                      <span>引导会话</span>
                                    </button>
                                    {visibleChats.map((item) => (
                                      <button
                                        key={item.id}
                                        title={item.title}
                                        className={
                                          current && conversationId === item.id
                                            ? "active"
                                            : ""
                                        }
                                        aria-current={
                                          current && conversationId === item.id
                                            ? "page"
                                            : undefined
                                        }
                                        onClick={() =>
                                          navigate(() =>
                                            onConversation?.(stage.id, item.id),
                                          )
                                        }
                                      >
                                        <span>{item.title}</span>
                                      </button>
                                    ))}
                                    {chats.length > RECENT_CHAT_COUNT && (
                                      <button
                                        className="tea-node-more-conversations"
                                        aria-label={`${showingAll ? "收起" : "展开"}${stage.name}的会话`}
                                        aria-expanded={showingAll}
                                        onClick={() =>
                                          setAllChats((previous) => {
                                            const next = new Set(previous);
                                            if (next.has(stage.id))
                                              next.delete(stage.id);
                                            else next.add(stage.id);
                                            return next;
                                          })
                                        }
                                      >
                                        <span>
                                          {showingAll ? "收起" : "展开显示"}
                                        </span>
                                      </button>
                                    )}
                                  </CollapsibleContent>
                                </section>
                              </Collapsible>
                            );
                          })}
                          {(record?.stages.length ?? 0) > 1 && (
                            <button
                              className="tea-more-nodes"
                              aria-label={
                                more ? "收起其他节点" : "展开其他节点"
                              }
                              aria-expanded={more}
                              onClick={() => {
                                setMore((value) => !value);
                                if (more) setExpanded(stageId);
                              }}
                            >
                              {more ? "收起其他节点" : "查看更多节点"}
                            </button>
                          )}
                        </SidebarGroup>
                      </CollapsibleContent>
                    )}
                  </Collapsible>
                );
              })}
            </CollapsibleContent>
          </Collapsible>
        ) : (
          <SidebarGroup>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  tooltip="当前业务"
                  aria-label="当前业务"
                  onClick={() => navigate(() => onNavigate("workspace"))}
                >
                  <FolderOpen />
                  <span>当前业务</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroup>
        )}
      </SidebarContent>
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              isActive={view === "library"}
              aria-current={view === "library" ? "page" : undefined}
              tooltip="资料库"
              aria-label="资料库"
              onClick={() => navigate(() => onNavigate("library"))}
            >
              <BookOpen />
              <span>资料库</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              isActive={view === "account"}
              tooltip="账号管理"
              aria-label="账号管理"
              onClick={() => navigate(() => onNavigate("account"))}
            >
              <UserRound />
              <span>{user.name}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail aria-label="切换侧栏" />
    </Sidebar>
  );
}
