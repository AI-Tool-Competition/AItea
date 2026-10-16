"use client";

import {
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  ChevronsUpDown,
  FolderClosed,
  FolderOpen,
  Globe2,
  LockKeyhole,
  MessageSquare,
  Plus,
  Settings2,
  UserRound,
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
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { type CaseDetail, type CaseSummary, type User } from "@/core/tea/types";

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

export function TeaNavigation({
  view,
  user,
  cases,
  caseId,
  record,
  recordLoading,
  stageId,
  libraryCaseId,
  onNavigate,
  onCase,
  onStage,
  onLibrary,
  onCreate,
}: {
  view: TeaView;
  user: User;
  cases: CaseSummary[];
  caseId: string;
  record?: CaseDetail;
  recordLoading: boolean;
  stageId: string;
  libraryCaseId: string;
  onNavigate: (view: TeaView) => void;
  onCase: (id: string) => void;
  onStage: (id: string) => void;
  onLibrary: (id: string) => void;
  onCreate: () => void;
}) {
  const { open, isMobile, setOpenMobile } = useSidebar();
  const [expandedCaseId, setExpandedCaseId] = useState<string | null>(caseId);
  useEffect(() => setExpandedCaseId(caseId), [caseId]);
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
            <SidebarMenuButton
              tooltip="新建出口业务"
              aria-label="新建出口业务"
              onClick={() => navigate(onCreate)}
            >
              <Plus />
              <span>新建出口业务</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        {!open && !isMobile && caseId && (
          <SidebarGroup className="pt-1">
            <SidebarMenu>
              {teaNavigationItems.slice(0, 2).map((item) => (
                <SidebarMenuItem key={item.id}>
                  <SidebarMenuButton
                    isActive={
                      view === item.id &&
                      (item.id !== "library" || libraryCaseId === caseId)
                    }
                    tooltip={item.label}
                    aria-label={item.label}
                    onClick={() =>
                      navigate(() =>
                        item.id === "library"
                          ? onLibrary(caseId)
                          : onCase(caseId),
                      )
                    }
                  >
                    <item.icon />
                    <span>{item.label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroup>
        )}
        {(open || isMobile) && (
          <SidebarGroup className="tea-business-group">
            <SidebarGroupLabel>我的出口业务</SidebarGroupLabel>
            <SidebarMenu className="tea-businesses">
              {cases.map((business) => (
                <SidebarMenuItem className="tea-business" key={business.id}>
                  <SidebarMenuButton
                    isActive={
                      caseId === business.id &&
                      (view === "workspace" ||
                        (view === "library" && libraryCaseId === business.id))
                    }
                    className="tea-business-title"
                    title={business.title}
                    onClick={() => {
                      setExpandedCaseId(business.id);
                      navigate(() => onCase(business.id));
                    }}
                  >
                    {caseId === business.id ? <FolderOpen /> : <FolderClosed />}
                    <span>{business.title}</span>
                  </SidebarMenuButton>
                  <SidebarMenuAction
                    aria-label={`${expandedCaseId === business.id ? "收起" : "展开"}业务：${business.title}`}
                    aria-expanded={expandedCaseId === business.id}
                    aria-controls={`tea-business-${business.id}`}
                    onClick={() => {
                      const expanding = expandedCaseId !== business.id;
                      setExpandedCaseId(expanding ? business.id : null);
                      if (expanding && caseId !== business.id)
                        onCase(business.id);
                    }}
                  >
                    {expandedCaseId === business.id ? (
                      <ChevronDown />
                    ) : (
                      <ChevronRight />
                    )}
                  </SidebarMenuAction>
                  {expandedCaseId === business.id && caseId === business.id && (
                    <div
                      className="tea-subconversations"
                      id={`tea-business-${business.id}`}
                      role="group"
                      aria-label={`${business.title}的对话与资料库`}
                    >
                      {record?.id === business.id &&
                        record.stages.map((stage) => (
                          <button
                            key={stage.id}
                            className={
                              stageId === stage.id && view === "workspace"
                                ? "active"
                                : ""
                            }
                            aria-current={
                              stageId === stage.id && view === "workspace"
                                ? "page"
                                : undefined
                            }
                            onClick={() => navigate(() => onStage(stage.id))}
                          >
                            <span className={`tea-tree-dot ${stage.status}`} />
                            <span>{stage.name}</span>
                            {stage.status === "locked" && (
                              <LockKeyhole size={12} />
                            )}
                            {stage.status === "completed" && (
                              <Check size={12} />
                            )}
                          </button>
                        ))}
                      {recordLoading && (
                        <span className="tea-business-loading" role="status">
                          正在读取对话…
                        </span>
                      )}
                      <button
                        className={
                          view === "library" && libraryCaseId === business.id
                            ? "tea-business-library active"
                            : "tea-business-library"
                        }
                        aria-current={
                          view === "library" && libraryCaseId === business.id
                            ? "page"
                            : undefined
                        }
                        onClick={() => navigate(() => onLibrary(business.id))}
                      >
                        <BookOpen size={14} />
                        <span>资料库</span>
                      </button>
                    </div>
                  )}
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroup>
        )}
      </SidebarContent>
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            {/* The mobile sidebar already owns the modal pointer lock. */}
            <DropdownMenu modal={!isMobile}>
              <DropdownMenuTrigger asChild>
                <SidebarMenuButton
                  isActive={
                    view === "account" ||
                    view === "studio" ||
                    view === "sources"
                  }
                  tooltip="账号与管理"
                  aria-label="账号与管理"
                  className="data-[state=open]:bg-sidebar-accent"
                >
                  <UserRound />
                  <span>{user.name}</span>
                  <ChevronsUpDown className="ml-auto" />
                </SidebarMenuButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                className="w-(--radix-dropdown-menu-trigger-width) min-w-56"
                side={isMobile || open ? "top" : "right"}
                align="end"
                sideOffset={4}
              >
                <DropdownMenuItem
                  onSelect={() => navigate(() => onNavigate("account"))}
                >
                  <UserRound />
                  账号管理
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onSelect={() => navigate(() => onNavigate("library"))}
                >
                  <BookOpen />
                  法规与我的资料
                </DropdownMenuItem>
                {teaNavigationItems.slice(2).map((item) => (
                  <DropdownMenuItem
                    key={item.id}
                    onSelect={() => navigate(() => onNavigate(item.id))}
                  >
                    <item.icon />
                    {item.label}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail aria-label="切换侧栏" />
    </Sidebar>
  );
}
