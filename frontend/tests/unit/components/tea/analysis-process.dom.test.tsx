import { afterEach, describe, expect, it } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { Trace } from "@/components/tea/shared";

afterEach(cleanup);
describe("analysis process presentation", () => {
  it("retains configured labels for step IDs that match object prototype names", () => {
    render(
      <Trace
        steps={[
          {
            id: "constructor",
            label: "核对业务资料",
            status: "completed",
            detail: "已核对资料",
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /依据与检查记录/ }));
    expect(screen.getByText("核对业务资料")).toBeTruthy();
  });
  it("distinguishes cached, skipped and stopped steps from newly completed work", () => {
    render(
      <Trace
        steps={[
          {
            id: "one",
            label: "资料检查",
            status: "cached",
            detail: "沿用已保存资料",
          },
          {
            id: "two",
            label: "条件检查",
            status: "skipped",
            detail: "本次不适用",
          },
          {
            id: "three",
            label: "报告检查",
            status: "cancelled",
            detail: "用户停止了本次分析",
          },
        ]}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: /分析已停止.*2\s*\/\s*3/ }),
    );
    expect(screen.getByText("沿用结果")).toBeTruthy();
    expect(screen.getByText("无需执行")).toBeTruthy();
    expect(screen.getByText("已停止")).toBeTruthy();
    expect(screen.queryByText("已完成")).toBeNull();
  });
});
