import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ContainerCreateWizard } from "../components/ContainerCreateWizard";
import { mockDashboardData } from "../lib/mockData";

afterEach(() => vi.unstubAllGlobals());

test("per-container maxima follow the selected server and reject oversized recipes", () => {
  const fetcher=vi.fn(); vi.stubGlobal("fetch",fetcher);
  render(<ContainerCreateWizard {...mockDashboardData} servers={[
    {...mockDashboardData.servers[0],cpuTotal:16,memoryTotalGb:64},
    {...mockDashboardData.servers[0],id:"server2",name:"Small host",cpuTotal:4,memoryTotalGb:8},
  ]} />);
  const cpu=screen.getByLabelText("CPU（核）");
  const memory=screen.getByLabelText("内存（GiB）");
  expect(cpu).toHaveAttribute("max","16");
  expect(memory).toHaveAttribute("max","64");
  fireEvent.change(cpu,{target:{value:"17"}});
  expect(screen.getByRole("button",{name:"创建容器",exact:true})).toBeDisabled();
  fireEvent.submit(cpu.closest("form")!);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.change(cpu,{target:{value:"16"}});
  fireEvent.change(memory,{target:{value:"64"}});
  expect(screen.getByRole("button",{name:"创建容器",exact:true})).not.toBeDisabled();
  fireEvent.change(screen.getByLabelText("服务器"),{target:{value:"server2"}});
  expect(cpu).toHaveAttribute("max","4");
  expect(memory).toHaveAttribute("max","8");
  expect(screen.getByRole("button",{name:"创建容器",exact:true})).toBeDisabled();
  expect(screen.getByText(/多个容器的配额可以超售/)).toBeVisible();
});
