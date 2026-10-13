import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ContainerCreateWizard } from "../components/ContainerCreateWizard";
import { mockDashboardData } from "../lib/mockData";

afterEach(() => vi.unstubAllGlobals());

test("creation requires an initial SSH password before contacting the server", () => {
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerCreateWizard {...mockDashboardData} />);
  const password = screen.getByLabelText("SSH 初始密码（至少 12 位）");
  fireEvent.submit(password.closest("form")!);
  expect(screen.getByText("请设置 12–128 位的 SSH 初始密码。")).toBeVisible();
  fireEvent.change(password, { target: { value: "short" } });
  fireEvent.submit(password.closest("form")!);
  expect(fetcher).not.toHaveBeenCalled();
});

test("creation sends the password once, starts the container and shows its campus command", async () => {
  const command = "ssh researcher@203.0.113.10 -p 23002";
  const fetcher = vi.fn<typeof fetch>(async () => Response.json({ container: { name: "debug", status: "running", ssh_command: command } }, { status: 201 }));
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerCreateWizard {...mockDashboardData} />);
  const password = screen.getByLabelText("SSH 初始密码（至少 12 位）");
  fireEvent.change(password, { target: { value: "my-initial-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "创建容器" }));
  await waitFor(() => expect(screen.getByText(new RegExp(command))).toBeVisible());
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toMatchObject({ ssh_password: "my-initial-ssh-password", auto_start: true });
  expect(password).toHaveValue("");
});
