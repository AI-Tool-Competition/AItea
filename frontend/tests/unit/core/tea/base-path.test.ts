import { afterEach, expect, rs, test } from "@rstest/core";

import {
  documentImageUrl,
  downloadUrl,
  streamMessage,
  teaRequest,
} from "@/core/tea/api";
import { normalizeTeaBasePath, teaResourceUrl } from "@/tea-paths";

const previous = process.env.NEXT_PUBLIC_TEA_BASE_PATH;
afterEach(() => {
  rs.restoreAllMocks();
  if (previous === undefined) delete process.env.NEXT_PUBLIC_TEA_BASE_PATH;
  else process.env.NEXT_PUBLIC_TEA_BASE_PATH = previous;
});

test.each(["", "/aitea", "/services/aitea"])(
  "requests and authenticated resources stay within deployment prefix %s",
  async (prefix) => {
    process.env.NEXT_PUBLIC_TEA_BASE_PATH = prefix;
    const fetch = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ status: "ok" })));
    await teaRequest("/bootstrap");
    expect(fetch).toHaveBeenCalledWith(
      `${prefix}/api/tea/bootstrap`,
      expect.objectContaining({
        credentials: "same-origin",
        cache: "no-store",
      }),
    );
    expect(downloadUrl("file/1")).toBe(
      `${prefix}/api/tea/documents/file%2F1/download`,
    );
    expect(documentImageUrl("file", "image/1")).toBe(
      `${prefix}/api/tea/documents/file/images/image%2F1`,
    );
    const resource = `${prefix}/api/tea/artifacts/report/download`;
    expect(teaResourceUrl("/api/tea/artifacts/report/download")).toBe(resource);
    expect(teaResourceUrl(resource)).toBe(resource);
    expect(teaResourceUrl("https://official.example/aitea")).toBe(
      "https://official.example/aitea",
    );
    expect(teaResourceUrl("#source-1")).toBe("#source-1");
  },
);

test("streaming under /aitea still delivers deltas before the stream closes", async () => {
  process.env.NEXT_PUBLIC_TEA_BASE_PATH = "/aitea";
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(value) {
      controller = value;
    },
  });
  const fetch = rs.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(stream, {
      headers: { "Content-Type": "text/event-stream" },
    }),
  );
  const received = rs.fn();
  const result = streamMessage(
    "case",
    { stage_id: "stage", content: "测试", client_message_id: "request" },
    { onEvent: received },
  );
  controller.enqueue(
    new TextEncoder().encode('event: delta\ndata: {"text":"首段"}\n\n'),
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(received).toHaveBeenCalledWith({
    event: "delta",
    data: { text: "首段" },
  });
  expect(fetch).toHaveBeenCalledWith(
    "/aitea/api/tea/cases/case/messages/stream",
    expect.anything(),
  );
  const done = { messages: [], case: { id: "case" } };
  controller.enqueue(
    new TextEncoder().encode(`event: done\ndata: ${JSON.stringify(done)}\n\n`),
  );
  await expect(result).resolves.toEqual(done);
});

test.each([
  "https://other.example",
  "//other",
  "/aitea?token=x",
  "/aitea/../admin",
])("invalid prefix %s fails instead of redirecting credentials", (prefix) =>
  expect(() => normalizeTeaBasePath(prefix)).toThrow(),
);
