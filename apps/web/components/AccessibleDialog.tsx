"use client";

import { X } from "lucide-react";
import {
  type ReactNode,
  type RefObject,
  useEffect,
  useId,
  useRef,
} from "react";


type AccessibleDialogProps = {
  title: string;
  children: ReactNode;
  onClose: () => void;
  triggerRef?: RefObject<HTMLElement | null>;
  className?: string;
  destructiveBusy?: boolean;
};

const focusableSelector = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

let openDialogs = 0;
let originalOverflow = "";

export function AccessibleDialog({
  title,
  children,
  onClose,
  triggerRef,
  className,
  destructiveBusy = false,
}: AccessibleDialogProps) {
  const titleId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  const busyRef = useRef(destructiveBusy);
  onCloseRef.current = onClose;
  busyRef.current = destructiveBusy;

  useEffect(() => {
    if (openDialogs === 0) originalOverflow = document.body.style.overflow;
    openDialogs += 1;
    document.body.style.overflow = "hidden";
    const restoreFocus = triggerRef?.current
      ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const dialog = dialogRef.current;
    const firstFocusable = dialog?.querySelector<HTMLElement>(focusableSelector);
    (firstFocusable ?? dialog)?.focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented) return;
      const currentDialog = dialogRef.current;
      if (!currentDialog) return;
      const dialogs = document.querySelectorAll('[role="dialog"][aria-modal="true"]');
      if (dialogs[dialogs.length - 1] !== currentDialog) return;
      if (event.key === "Escape" && !busyRef.current) {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = Array.from(
        currentDialog.querySelectorAll<HTMLElement>(focusableSelector),
      ).filter((element) => !element.hasAttribute("hidden"));
      if (focusable.length === 0) {
        event.preventDefault();
        currentDialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      openDialogs -= 1;
      if (openDialogs === 0) document.body.style.overflow = originalOverflow;
      restoreFocus?.focus();
    };
  }, [triggerRef]);

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (
          event.currentTarget === event.target
          && !destructiveBusy
        ) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className={`dialog${className ? ` ${className}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="dialog-heading">
          <h2 id={titleId}>{title}</h2>
          <button
            className="icon-button"
            type="button"
            aria-label={`关闭${title}`}
            title="关闭"
            disabled={destructiveBusy}
            onClick={onClose}
          >
            <X size={17} aria-hidden="true" />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}
