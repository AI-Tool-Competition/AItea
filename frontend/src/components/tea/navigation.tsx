"use client";

import {
  Check,
  ChevronDown,
  FolderClosed,
  FolderOpen,
  LockKeyhole,
  MessageSquare,
  Plus,
  Settings2,
  UserRound,
  BookOpen,
  Globe2,
  FileText,
} from "lucide-react";
import { useEffect, useState } from "react";

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
  libraryCaseId?: string;
  conversations?: NodeConversation[];
  conversationId?: string;
  conversationBusy?: boolean;
  onNavigate: (view: TeaView) => void;
  onCase: (id: string) => void;
  onStage: (id: string) => void;
  onLibrary?: (id: string) => void;
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
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set([stageId]),
  );
  useEffect(() => {
    setExpanded((previous) => new Set([...previous, stageId]));
  }, [stageId]);
  const business = cases.find((item) => item.id === caseId);
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
        <SidebarMenu>
          <SidebarMenuItem>
            <DropdownMenu modal={!isMobile}>
              <DropdownMenuTrigger asChild>
                <SidebarMenuButton
                  className="tea-business-switch"
                  tooltip={business?.title ?? "选择出口业务"}
                  aria-label="切换业务"
                >
                  <FolderOpen />
                  <span className="tea-business-switch-name">
                    {business?.title ?? "选择出口业务"}
                  </span>
                  <ChevronDown className="ml-auto shrink-0" />
                </SidebarMenuButton>
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
                    <span className="tea-business-option">{item.title}</span>
                    {item.id === caseId && <Check size={14} />}
                  </DropdownMenuItem>
                ))}
                {cases.length > 0 && <DropdownMenuSeparator />}
                <DropdownMenuItem onSelect={() => navigate(onCreate)}>
                  <Plus size={15} />
                  新建业务
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        {open || isMobile ? (
          <SidebarGroup className="tea-node-folders" aria-label="当前业务节点">
            {recordLoading && (
              <p role="status" className="tea-business-loading">
                正在读取节点…
              </p>
            )}
            {stages.map((stage) => {
              const current = stage.id === stageId && view === "workspace";
              const opened = expanded.has(stage.id);
              return (
                <section
                  key={stage.id}
                  className="tea-node-folder"
                  data-current={current}
                >
                  <div className="tea-node-folder-heading">
                    <button
                      className="tea-node-folder-toggle"
                      aria-label={`${opened ? "收起" : "展开"}节点：${stage.name}`}
                      aria-expanded={opened}
                      aria-controls={`node-${stage.id}`}
                      onClick={() =>
                        setExpanded((previous) => {
                          const next = new Set(previous);
                          if (next.has(stage.id)) next.delete(stage.id);
                          else next.add(stage.id);
                          return next;
                        })
                      }
                    >
                      <ChevronDown
                        size={13}
                        className={opened ? "" : "-rotate-90"}
                      />
                    </button>
                    <button
                      className="tea-node-folder-name"
                      title={stage.name}
                      onClick={() => {
                        setExpanded(
                          (previous) => new Set([...previous, stage.id]),
                        );
                        navigate(() => onStage(stage.id));
                      }}
                    >
                      {opened ? (
                        <FolderOpen size={15} />
                      ) : (
                        <FolderClosed size={15} />
                      )}
                      <span>{stage.name}</span>
                      {stage.status === "locked" ? (
                        <LockKeyhole size={12} />
                      ) : stage.status === "completed" ? (
                        <Check size={12} />
                      ) : (
                        <i className="tea-node-active-dot" />
                      )}
                    </button>
                  </div>
                  {opened && (
                    <div
                      id={`node-${stage.id}`}
                      className="tea-node-entries"
                      role="group"
                      aria-label={`${stage.name}的会话`}
                    >
                      <button
                        className={
                          current && conversationId === "guide" ? "active" : ""
                        }
                        aria-current={
                          current && conversationId === "guide"
                            ? "page"
                            : undefined
                        }
                        onClick={() => navigate(() => onStage(stage.id))}
                      >
                        <FileText size={14} />
                        <span>引导会话</span>
                      </button>
                      {conversations
                        .filter(
                          (item) =>
                            item.stage_id === stage.id && item.kind !== "guide",
                        )
                        .map((item) => (
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
                            <MessageSquare size={13} />
                            <span>{item.title}</span>
                          </button>
                        ))}
                      <button
                        className="tea-new-conversation"
                        disabled={stage.status === "locked" || conversationBusy}
                        onClick={() =>
                          navigate(() => onCreateConversation?.(stage.id))
                        }
                      >
                        <Plus size={14} />
                        <span>
                          {conversationBusy && current
                            ? "正在新建…"
                            : "新建会话"}
                        </span>
                      </button>
                    </div>
                  )}
                </section>
              );
            })}
            {(record?.stages.length ?? 0) > 1 && (
              <button
                className="tea-more-nodes"
                aria-expanded={more}
                onClick={() => setMore((value) => !value)}
              >
                <ChevronDown size={14} className={more ? "rotate-180" : ""} />
                {more ? "收起其他节点" : "查看更多节点"}
              </button>
            )}
          </SidebarGroup>
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
