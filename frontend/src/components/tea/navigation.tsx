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
  LockKeyhole,
  MessageSquare,
  Plus,
  Settings2,
  Upload,
  UserRound,
} from "lucide-react";

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
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
  { id: "studio", icon: Settings2, label: "法务工作台" },
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
  onUpload,
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
  onUpload: () => void;
}) {
  const { open, isMobile, setOpenMobile } = useSidebar();
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
            {teaNavigationItems.map((item) => (
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
            <SidebarMenuItem>
              <SidebarMenuButton
                tooltip="上传资料"
                aria-label="上传资料"
                onClick={() => navigate(onUpload)}
              >
                <Upload />
                <span>上传资料</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarGroup>
        {(open || isMobile) && (
          <SidebarGroup className="tea-business-group">
            <SidebarGroupLabel>我的出口业务</SidebarGroupLabel>
            <div className="tea-businesses">
              {cases.map((business) => (
                <div className="tea-business" key={business.id}>
                  <SidebarMenuButton
                    isActive={caseId === business.id && view === "workspace"}
                    className="tea-business-title"
                    onClick={() => navigate(() => onCase(business.id))}
                  >
                    {caseId === business.id ? <FolderOpen /> : <FolderClosed />}
                    <span>{business.title}</span>
                    {caseId === business.id ? (
                      <ChevronDown />
                    ) : (
                      <ChevronRight />
                    )}
                  </SidebarMenuButton>
                  {caseId === business.id && record?.id === business.id && (
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
                          {stage.status === "completed" && <Check size={12} />}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </SidebarGroup>
        )}
      </SidebarContent>
      <SidebarFooter>
        {(open || isMobile) && (
          <div className="tea-navigation-route">
            <span>演示路线</span>
            中国 <ArrowRight size={12} /> 印度尼西亚
          </div>
        )}
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
