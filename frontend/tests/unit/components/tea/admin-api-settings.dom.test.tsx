import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Link from "next/link";

import { AdminApiSettings } from "@/components/tea/admin-api-settings";
import { type ApiSettings } from "@/core/tea/types";

const { push } = rs.hoisted(() => ({ push: rs.fn() }));
rs.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
  push.mockClear();
});

const initial: ApiSettings = {
  revision: "a".repeat(64),
  agent: {
    base_url: "https://agent.example/v1",
    model: "agent-model",
    api_key_configured: true,
  },
  embedding: {
    base_url: "https://embed.example/v1",
    model: "embed-model",
    api_key_configured: true,
  },
  rerank: { base_url: "", model: "", api_key_configured: false },
  mineru: { api_key_configured: true },
  agent_inherits_api_key: false,
  embedding_inherits_api_key: false,
  environment_overrides: [],
  restart_required: false,
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}

function input(name: string) {
  return screen.getByLabelText<HTMLInputElement>(name);
}

describe("admin API settings", () => {
  it("shows all providers without loading existing keys into inputs", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(json(initial));
    render(<AdminApiSettings />);
    await screen.findByDisplayValue("agent-model");
    for (const name of ["Agent", "Embedding", "Rerank", "MinerU"]) {
      expect(input(`${name} API Key`).value).toBe("");
      expect(input(`${name} API Key`).type).toBe("password");
    }
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "保存配置" })
        .disabled,
    ).toBe(true);
  });

  it("sends only changed fields and explicit key removal, then clears new key inputs", async () => {
    const next = {
      ...initial,
      revision: "b".repeat(64),
      restart_required: true,
    };
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(initial))
      .mockResolvedValueOnce(json(next));
    render(<AdminApiSettings />);
    await screen.findByDisplayValue("agent-model");
    fireEvent.change(input("Agent 调用模型"), {
      target: { value: "next-model" },
    });
    fireEvent.change(input("Rerank API Key"), {
      target: { value: "new-private-key" },
    });
    fireEvent.click(input("清除 MinerU API Key"));
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await screen.findByText(/配置已保存。修改检索模型/);
    expect(JSON.parse(fetcher.mock.calls[1]![1]!.body as string)).toEqual({
      expected_revision: initial.revision,
      agent: { model: "next-model" },
      rerank: { api_key: "new-private-key" },
      mineru: { clear_api_key: true },
    });
    expect(input("Rerank API Key").value).toBe("");
    expect(input("清除 MinerU API Key").checked).toBe(false);
    expect(screen.getByText(/等待重启生效/)).toBeTruthy();
  });

  it("keeps edits and keys after a conflict and rebases on an explicitly refreshed revision", async () => {
    const refreshed = {
      ...initial,
      revision: "c".repeat(64),
      embedding: { ...initial.embedding, model: "another-admin-model" },
    };
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(initial))
      .mockResolvedValueOnce(
        json(
          {
            error: {
              code: "stale_api_settings",
              message: "配置已被其他管理员修改",
            },
          },
          409,
        ),
      )
      .mockResolvedValueOnce(json(refreshed))
      .mockResolvedValueOnce(json({ ...refreshed, revision: "d".repeat(64) }));
    render(<AdminApiSettings />);
    await screen.findByDisplayValue("agent-model");
    fireEvent.change(input("Agent 调用模型"), {
      target: { value: "my-model" },
    });
    fireEvent.change(input("MinerU API Key"), {
      target: { value: "keep-my-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await screen.findByRole("alert");
    expect(input("Agent 调用模型").value).toBe("my-model");
    expect(input("MinerU API Key").value).toBe("keep-my-key");
    fireEvent.click(screen.getByRole("button", { name: "读取最新版本" }));
    await screen.findByDisplayValue("another-admin-model");
    expect(input("Agent 调用模型").value).toBe("my-model");
    expect(input("MinerU API Key").value).toBe("keep-my-key");
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await screen.findByText(/配置已保存。修改检索模型/);
    expect(JSON.parse(fetcher.mock.calls[3]![1]!.body as string)).toEqual({
      expected_revision: refreshed.revision,
      agent: { model: "my-model" },
      mineru: { api_key: "keep-my-key" },
    });
  });

  it("offers a retry after loading fails and disables launcher-owned fields", async () => {
    rs.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(
        json({
          ...initial,
          environment_overrides: ["AGENT_MODEL", "MINERU_API_TOKEN"],
        }),
      );
    render(<AdminApiSettings />);
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByDisplayValue("agent-model");
    expect(input("Agent 调用模型").disabled).toBe(true);
    expect(input("MinerU API Key").disabled).toBe(true);
    expect(input("清除 MinerU API Key").disabled).toBe(true);
    expect(input("Agent Base URL").disabled).toBe(false);
  });

  it("aborts obsolete reads when the account's component is replaced", async () => {
    let firstSignal: AbortSignal | undefined;
    let resolveFirst: ((response: Response) => void) | undefined;
    rs.spyOn(globalThis, "fetch")
      .mockImplementationOnce((_url, options) => {
        firstSignal = options?.signal ?? undefined;
        return new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        });
      })
      .mockResolvedValueOnce(
        json({
          ...initial,
          agent: { ...initial.agent, model: "current-user-model" },
        }),
      );
    const view = render(<AdminApiSettings key="alice" />);
    view.rerender(<AdminApiSettings key="bob" />);
    await screen.findByDisplayValue("current-user-model");
    expect(firstSignal?.aborted).toBe(true);
    resolveFirst?.(json(initial));
    await waitFor(() =>
      expect(input("Agent 调用模型").value).toBe("current-user-model"),
    );
  });

  it("warns before navigating away with unsaved credentials", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(json(initial));
    render(
      <>
        <Link href="/tea" prefetch={false}>
          返回工作台
        </Link>
        <AdminApiSettings />
      </>,
    );
    await screen.findByDisplayValue("agent-model");
    fireEvent.change(input("MinerU API Key"), { target: { value: "new-key" } });
    fireEvent.click(screen.getByRole("link", { name: "返回工作台" }));
    await screen.findByRole("dialog", { name: "离开 API 配置？" });
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(input("MinerU API Key").value).toBe("new-key");
    expect(push).not.toHaveBeenCalled();
  });
});
