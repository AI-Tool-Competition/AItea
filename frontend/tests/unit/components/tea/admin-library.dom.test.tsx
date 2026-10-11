import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { AdminLibrary } from "@/components/tea/admin-library";
import { type Document } from "@/core/tea/types";

afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});
const parent: Document = {
  id: "primary",
  title: "产品说明",
  filename: "产品.md",
  role: "primary",
  status: "ready",
  mime_type: "text/markdown",
  size: 100,
  version: 1,
  case_id: null,
  parent_id: null,
  tags: [],
  created_at: "2026-10-11T00:00:00Z",
  source_url: null,
  error: null,
  chunk_count: 1,
};
describe("admin library", () => {
  it("refreshes the open secondary list after an attachment upload without losing the selected view", async () => {
    let uploaded = false;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const path =
        typeof url === "string" ? url : url instanceof URL ? url.href : url.url;
      const payload = path.includes("parent_id=")
        ? {
            items: uploaded
              ? [
                  {
                    ...parent,
                    id: "secondary",
                    parent_id: parent.id,
                    role: "secondary",
                    status: "stored",
                    title: "新关联附件",
                  },
                ]
              : [],
            total: uploaded ? 1 : 0,
          }
        : path.includes("/documents/primary")
          ? {
              document: parent,
              elements: [],
              chunks: [],
              element_total: 0,
              chunk_total: 0,
              warnings: [],
            }
          : { items: [parent], total: 1 };
      return new Response(JSON.stringify(payload), { status: 200 });
    });
    const props = { cases: [], onUpload: () => undefined };
    const view = render(<AdminLibrary {...props} revision={0} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "查看拆解" })).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "查看拆解" }));
    fireEvent.click(screen.getByRole("tab", { name: "二级资料" }));
    await waitFor(() =>
      expect(screen.getByText(/尚未添加二级资料/)).toBeTruthy(),
    );
    uploaded = true;
    view.rerender(<AdminLibrary {...props} revision={1} />);
    await waitFor(() => expect(screen.getByText("新关联附件")).toBeTruthy());
    expect(
      screen
        .getByRole("tab", { name: "二级资料" })
        .getAttribute("aria-selected"),
    ).toBe("true");
  });
});
