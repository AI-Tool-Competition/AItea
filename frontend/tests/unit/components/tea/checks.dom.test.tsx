import { afterEach, describe, expect, it } from "@rstest/core";
import { cleanup, render, screen } from "@testing-library/react";

import { Checks } from "@/components/tea/shared";

afterEach(cleanup);

describe("task checks", () => {
  it("identifies repeated conditions by task while keeping single-task checks readable", () => {
    const shared = {
      id: "confirmation",
      label: "确认当前资料",
      status: "missing" as const,
      reason: "请确认",
      evidence_refs: [],
    };
    const view = render(
      <Checks
        checks={[
          { ...shared, task_id: "product", task_title: "整理产品信息" },
          { ...shared, task_id: "company", task_title: "确认企业与联系人" },
        ]}
      />,
    );
    expect(screen.getByText("整理产品信息")).toBeTruthy();
    expect(screen.getByText("确认企业与联系人")).toBeTruthy();
    expect(screen.getAllByText("确认当前资料")).toHaveLength(2);
    view.rerender(<Checks checks={[shared]} />);
    expect(screen.getByText("确认当前资料")).toBeTruthy();
    expect(screen.queryByText("整理产品信息")).toBeNull();
  });
});
