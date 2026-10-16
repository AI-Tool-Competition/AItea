"use client";

import { ChevronDown, Scale, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { ChatSkill } from "@/core/tea/types";

export const chatSkills: { id: ChatSkill; label: string }[] = [
  { id: "guidance", label: "办理指导" },
  { id: "materials", label: "资料核对" },
  { id: "legal", label: "法律分析" },
  { id: "draft", label: "文稿起草" },
  { id: "handoff", label: "节点交接" },
];
export function ChatSkills({
  skills,
  onSkills,
  legal,
  onLegal,
}: {
  skills: ChatSkill[];
  onSkills: (value: ChatSkill[]) => void;
  legal: boolean;
  onLegal: (value: boolean) => void;
}) {
  return (
    <div className="tea-chat-skills">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" size="sm" variant="ghost">
            <Sparkles size={14} />
            {skills.length ? `已选 ${skills.length} 项能力` : "智能调用"}
            <ChevronDown size={12} />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="tea-chat-skills-menu">
          <DropdownMenuLabel>默认按问题自动组合，也可以指定</DropdownMenuLabel>
          {chatSkills.map((skill) => (
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
