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
import { useState } from "react";

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
  stageId,
  onNavigate,
  onCase,
  onStage,
  onCreate,
}: {
  view: TeaView;
  user: User;
  cases: CaseSummary[];
  caseId: string;
  record?: CaseDetail;
  stageId: string;
  onNavigate: (view: TeaView) => void;
  onCase: (id: string) => void;
  onStage: (id: string) => void;
  onCreate: () => void;
}) {
  const { open, isMobile, setOpenMobile } = useSidebar();
  const [expandedCaseId, setExpandedCaseId] = useState<string | null>(null);
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
        <SidebarGroup className="pt-1">
          <SidebarMenu>
            {teaNavigationItems.slice(0, 2).map((item) => (
              <SidebarMenuItem key={item.id}>
                <SidebarMenuButton
                  isActive={view === item.id}
                  tooltip={item.label}
                  aria-label={item.label}
                  onClick={() => navigate(() => onNavigate(item.id))}
                >
                  <item.icon />
                  <span>{item.label}</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            ))}
          </SidebarMenu>
        </SidebarGroup>
        {(open || isMobile) && (
          <SidebarGroup className="tea-business-group">
            <SidebarGroupLabel>我的出口业务</SidebarGroupLabel>
            <SidebarMenu className="tea-businesses">
              {cases.map((business) => (
                <SidebarMenuItem className="tea-business" key={business.id}>
                  <SidebarMenuButton
                    isActive={caseId === business.id && view === "workspace"}
                    className="tea-business-title"
                    onClick={() => navigate(() => onCase(business.id))}
                  >
                    {caseId === business.id ? <FolderOpen /> : <FolderClosed />}
                    <span>{business.title}</span>
                  </SidebarMenuButton>
                  <SidebarMenuAction
                    aria-label={`${expandedCaseId === business.id ? "收起" : "展开"}业务阶段：${business.title}`}
                    aria-expanded={expandedCaseId === business.id}
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
                  {expandedCaseId === business.id &&
                    caseId === business.id &&
                    record?.id === business.id && (
                      <div className="tea-subconversations">
                        {record.stages.map((stage) => (
                          <button
                            key={stage.id}
                            className={
                              stageId === stage.id && view === "workspace"
                                ? "active"
                                : ""
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
