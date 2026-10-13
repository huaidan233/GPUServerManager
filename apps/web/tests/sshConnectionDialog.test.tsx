import { fireEvent, render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { SSHConnectionDialog } from "../components/SSHConnectionDialog";

afterEach(() => vi.unstubAllGlobals());

const props = { containerId: "container-1", sshCommand: "ssh researcher@203.0.113.10 -p 22001", onClose: vi.fn() };

async function issue(validBefore: string | null) {
  const fetcher = vi.fn().mockResolvedValue(Response.json({
    certificate: "test-certificate", principal: "gsm:member-1:container-1",
    valid_after: "2026-10-13T00:00:00Z", valid_before: validBefore,
  }));
  vi.stubGlobal("fetch", fetcher);
  render(<SSHConnectionDialog {...props} />);
  fireEvent.change(screen.getByLabelText("本地 SSH 公钥"), { target: { value: "ssh-ed25519 test-public-key" } });
  fireEvent.click(screen.getByRole("button", { name: "签发永久证书" }));
  await screen.findByRole("heading", { name: "证书文件" });
  return fetcher;
}

test("permanent certificate displays no expiry and copies a command with its own certificate and private key", async () => {
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: copy } });
  const fetcher = await issue(null);
  expect(screen.getByText(/永久有效，无需续签/)).toBeVisible();
  expect(screen.queryByText(/1970|Invalid Date|证书有效期至/)).not.toBeInTheDocument();
  expect(fetcher.mock.calls[0][0]).toBe("/api/gsm/containers/container-1/ssh-certificate");
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ public_key: "ssh-ed25519 test-public-key" });
  fireEvent.click(within(screen.getByRole("region", { name: "连接命令" })).getByRole("button", { name: "复制" }));
  await screen.findByText("连接命令已复制");
  expect(copy).toHaveBeenCalledWith("ssh -i ~/.ssh/gsm_member -o CertificateFile=~/.ssh/gsm_member-container-1-cert.pub researcher@203.0.113.10 -p 22001");
});

test("a response from an older API preserves its actual expiry", async () => {
  await issue("2026-10-13T00:15:00Z");
  expect(screen.getByText(/这是旧版短期证书，到期后需重新签发/)).toBeVisible();
  expect(screen.queryByText(/永久有效，无需续签/)).not.toBeInTheDocument();
});
