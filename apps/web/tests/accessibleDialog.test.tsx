import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { useRef, useState } from "react";
import { test } from "vitest";

import { AccessibleDialog } from "../components/AccessibleDialog";


function DialogHarness() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button ref={triggerRef} type="button" onClick={() => setOpen(true)}>
        打开
      </button>
      {open && (
        <AccessibleDialog
          title="测试对话框"
          triggerRef={triggerRef}
          onClose={() => setOpen(false)}
        >
          <button type="button">第一个操作</button>
          <button type="button">最后一个操作</button>
        </AccessibleDialog>
      )}
    </>
  );
}


test("dialog traps focus, closes on escape, and restores trigger focus", async () => {
  render(<DialogHarness />);
  const trigger = screen.getByRole("button", { name: "打开" });
  trigger.focus();
  fireEvent.click(trigger);

  const dialog = screen.getByRole("dialog", { name: "测试对话框" });
  await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement));

  const last = screen.getByRole("button", { name: "最后一个操作" });
  last.focus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(screen.getByRole("button", { name: "关闭测试对话框" })).toHaveFocus();

  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(trigger).toHaveFocus();
});
