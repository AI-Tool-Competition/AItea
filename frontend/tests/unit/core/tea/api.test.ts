import { afterEach, describe, expect, it, rs } from "@rstest/core";

import { teaRequest, TeaApiError } from "@/core/tea/api";

describe("Tea API transport", () => {
  afterEach(() => {
    rs.restoreAllMocks();
  });
  it("preserves API conflict messages and status so drafts can be retained", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "stale_revision",
            message: "资料已更新，请刷新后核验",
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    await expect(teaRequest("/cases/example")).rejects.toMatchObject({
      status: 409,
      code: "stale_revision",
      message: "资料已更新，请刷新后核验",
    });
  });
  it("handles an expired session even when the response body is unavailable", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("", { status: 401 }),
    );
    await expect(teaRequest("/bootstrap")).rejects.toBeInstanceOf(TeaApiError);
  });
  it("accepts successful empty logout responses", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    await expect(
      teaRequest("/auth/logout", { method: "POST" }),
    ).resolves.toBeUndefined();
  });
});
