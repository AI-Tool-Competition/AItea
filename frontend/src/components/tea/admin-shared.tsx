"use client";

import { ArrowDown, ArrowUp, Trash2 } from "lucide-react";
import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function ConfirmAction({
  title,
  description,
  onConfirm,
  children,
  disabled = false,
}: {
  title: string;
  description: string;
  onConfirm: () => void;
  children: ReactNode;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        {children}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="tea-theme tea-dialog">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              取消
            </Button>
            <Button
              onClick={() => {
                onConfirm();
                setOpen(false);
              }}
            >
              确认
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
export function OrderActions({
  label,
  index,
  count,
  onMove,
  onRemove,
}: {
  label: string;
  index: number;
  count: number;
  onMove: (direction: -1 | 1) => void;
  onRemove?: () => void;
}) {
  return (
    <div className="ta-order-actions">
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={`${label}前移`}
        disabled={index === 0}
        onClick={() => onMove(-1)}
      >
        <ArrowUp size={14} />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={`${label}后移`}
        disabled={index === count - 1}
        onClick={() => onMove(1)}
      >
        <ArrowDown size={14} />
      </Button>
      {onRemove && (
        <ConfirmAction
          title={`删除${label}？`}
          description="只修改当前草稿。发布后，新建业务会采用修改后的配置。"
          onConfirm={onRemove}
        >
          <Trash2 size={14} />
          <span className="sr-only">删除{label}</span>
        </ConfirmAction>
      )}
    </div>
  );
}
