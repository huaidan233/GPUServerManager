import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import { FirstRunGuide } from "../components/FirstRunGuide";

test("shows the first-run guide only for administrators without servers", () => {
  const { rerender } = render(<FirstRunGuide isAdmin serverCount={0} />);
  expect(screen.getByRole("heading", { name: "开始配置 GPU Server Manager" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "打开系统设置" })).toHaveAttribute("href", "/?workspace=system");

  rerender(<FirstRunGuide isAdmin={false} serverCount={0} />);
  expect(screen.queryByRole("heading", { name: "开始配置 GPU Server Manager" })).not.toBeInTheDocument();
  rerender(<FirstRunGuide isAdmin serverCount={1} />);
  expect(screen.queryByRole("heading", { name: "开始配置 GPU Server Manager" })).not.toBeInTheDocument();
});
