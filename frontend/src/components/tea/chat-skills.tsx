"use client";

import { ChevronDown, Scale, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { teaRequest } from "@/core/tea/api";
import type { AgentCatalog, ChatSkill } from "@/core/tea/types";

export const chatSkills: { id: ChatSkill; label: string }[] = [
  { id: "guidance", label: "办理指导" },
  { id: "materials", label: "资料核对" },
  { id: "legal", label: "法律分析" },
  { id: "draft", label: "文稿起草" },
  { id: "handoff", label: "节点交接" },
  { id: "research", label: "联网调研" },
  { id: "vision", label: "图片核对" },
];
export function ChatSkills({
  skills,
  onSkills,
  legal,
  onLegal,
  caseId,
  stageId,
}: {
  skills: ChatSkill[];
  onSkills: (value: ChatSkill[]) => void;
  legal: boolean;
  onLegal: (value: boolean) => void;
  caseId: string;
  stageId: string;
}) {
  const [catalog, setCatalog] = useState<AgentCatalog | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setCatalog(null);
    setFailed(false);
    void teaRequest<AgentCatalog>(
      `/cases/${caseId}/stages/${stageId}/agent-tools`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (
          !value ||
          !Array.isArray(value.skills) ||
          !Array.isArray(value.combinations) ||
          !Array.isArray(value.tools) ||
          !value.skills.every(
            (skill) =>
              chatSkills.some((known) => known.id === skill.id) &&
              typeof skill.label === "string",
          ) ||
          !value.combinations.every(
            (item) =>
              typeof item.label === "string" &&
              typeof item.description === "string" &&
              Array.isArray(item.skills) &&
              item.skills.every((id) =>
                chatSkills.some((known) => known.id === id),
              ),
          )
        ) {
          throw new Error("invalid tool catalog");
        }
        if (!controller.signal.aborted) setCatalog(value);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => controller.abort();
  }, [caseId, stageId, retry, open]);
  return (
    <div className="tea-chat-skills">
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger asChild>
          <Button type="button" size="sm" variant="ghost">
            <Sparkles size={14} />
            {skills.length ? `已选 ${skills.length} 项能力` : "智能调用"}
            <ChevronDown size={12} />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="tea-chat-skills-menu">
          <DropdownMenuLabel>独立工具，按问题灵活组合</DropdownMenuLabel>
          {(catalog?.skills ?? chatSkills).map((skill) => (
            <DropdownMenuCheckboxItem
              key={skill.id}
              checked={skills.includes(skill.id)}
              onSelect={(event) => event.preventDefault()}
              onCheckedChange={(checked) =>
                onSkills(
                  checked
                    ? [...skills, skill.id]
                    : skills.filter((id) => id !== skill.id),
                )
              }
            >
              {skill.label}
            </DropdownMenuCheckboxItem>
          ))}
          {catalog && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>组合建议</DropdownMenuLabel>
              {catalog.combinations.map((combination) => (
                <DropdownMenuItem
                  key={combination.label}
                  onSelect={() => onSkills(combination.skills)}
                >
                  <span className="max-w-64 whitespace-normal">
                    <span className="block">{combination.label}</span>
                    <span className="text-muted-foreground text-xs">
                      {combination.description}
                    </span>
                  </span>
                </DropdownMenuItem>
              ))}
            </>
          )}
          {failed && (
            <DropdownMenuItem
              onSelect={(event) => {
                event.preventDefault();
                setRetry((value) => value + 1);
              }}
            >
              组合提示加载失败，点击重试
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-pressed={legal}
        onClick={() => onLegal(!legal)}
      >
        <Scale size={14} />
        法律分析
      </Button>
    </div>
  );
}
