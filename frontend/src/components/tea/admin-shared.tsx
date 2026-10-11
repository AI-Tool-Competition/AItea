"use client";

import { ArrowDown, ArrowUp, PanelRightClose, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function EditorInspector({
  open,
  onClose,
  label,
  children,
}: {
  open: boolean;
  onClose: () => void;
  label: string;
  children: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const [instant, setInstant] = useState(false);
  useEffect(() => {
    if (!open) return;
    if (
      document.activeElement instanceof HTMLElement &&
      document.activeElement.matches("[data-inspector-trigger]")
    )
      trigger.current = document.activeElement;
    panel.current?.focus({ preventScroll: true });
  }, [open]);
  useEffect(() => {
    // Confirmation dialogs render in a portal and own dismissal while open.
    const hasConfirmation = () =>
      document.querySelector('[data-admin-confirmation][data-state="open"]');
    function pointerDown(event: PointerEvent) {
      setInstant(false);
      if (!(event.target instanceof Element)) return;
      const target = event.target.closest<HTMLElement>(
        "[data-inspector-trigger]",
      );
      if (target) trigger.current = target;
      if (
        open &&
        !panel.current?.contains(event.target) &&
        !target &&
        !hasConfirmation()
      )
        onClose();
    }
    function keyDown(event: KeyboardEvent) {
      if (
        (event.key === "Enter" || event.key === " ") &&
        event.target instanceof Element &&
        event.target.closest("[data-inspector-trigger]")
      )
        setInstant(true);
      if (
        open &&
        event.key === "Escape" &&
        !event.defaultPrevented &&
        !hasConfirmation()
      ) {
        event.preventDefault();
        setInstant(true);
        onClose();
        if (trigger.current?.isConnected)
          trigger.current.focus({ preventScroll: true });
      }
    }
    document.addEventListener("pointerdown", pointerDown, true);
    document.addEventListener("keydown", keyDown);
    return () => {
      document.removeEventListener("pointerdown", pointerDown, true);
      document.removeEventListener("keydown", keyDown);
    };
  }, [open, onClose]);
  return (
    <aside
      ref={panel}
      className="ta-inspector"
      aria-label={label}
      aria-hidden={!open}
      inert={!open}
      data-state={open ? "open" : "closed"}
      data-instant={instant || undefined}
      tabIndex={-1}
    >
      <Button
        type="button"
        className="ta-inspector-close"
        size="icon-sm"
        variant="ghost"
        aria-label={`收起${label}`}
        onClick={() => {
          onClose();
          if (trigger.current?.isConnected)
            trigger.current.focus({ preventScroll: true });
        }}
      >
        <PanelRightClose size={17} />
      </Button>
      {children}
    </aside>
  );
}

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
        <DialogContent className="tea-theme tea-dialog" data-admin-confirmation>
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
