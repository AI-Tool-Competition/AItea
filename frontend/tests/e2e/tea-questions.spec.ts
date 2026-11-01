import { expect, test } from "@playwright/test";

import fixture from "../fixtures/tea-chat.json" with { type: "json" };

const entry = process.env.PLAYWRIGHT_TEA_BASE_PATH ?? "/";
const markdown = [
  "## 准备出口资料",
  "先核对主体、产品和目的地，再继续安排。",
  "1. 确认进口主体\n   - 核对企业名称\n   - 确认联系人\n2. 整理产品清单",
  "> 这里是待确认的信息，需要结合真实情况处理。",
  "使用 `product_id` 记录产品。",
  "| 资料 | 状态 |\n| --- | --- |\n| 产品清单 | 待核对 |\n| 采购合同 | 待补充 |",
  "```typescript\n" +
    Array.from(
      { length: 72 },
      (_, i) =>
        `const product${i} = \"${i === 0 ? "茶".repeat(160) : "六堡茶"}\";`,
    ).join("\n") +
    "\n```",
  "请先确认下面的事项。",
].join("\n\n");
const question = {
  id: "browser-question",
  questions: [
    {
      question: "这批茶的出口目的地是哪里？",
      multiple: false,
      options: [
        { label: "新加坡", description: "已有当地进口商" },
        { label: "印度尼西亚", description: "进口主体待确认" },
      ],
    },
    {
      question: "已经准备了哪些资料？",
      multiple: true,
      options: [
        { label: "产品清单", description: "" },
        { label: "采购合同", description: "" },
      ],
    },
    {
      question: "产品是什么形态？",
      multiple: false,
      options: [
        { label: "散茶", description: "" },
        { label: "紧压茶", description: "" },
      ],
    },
    {
      question: "计划采用哪些包装？",
      multiple: true,
      options: [
        { label: "散装", description: "" },
        { label: "零售小包装", description: "" },
      ],
    },
    {
      question: "首批数量是否确定？",
      multiple: false,
      options: [
        { label: "已经确定", description: "" },
        { label: "还在估算", description: "" },
      ],
    },
    {
      question: "预计何时发货？",
      multiple: false,
      options: [
        { label: "一个月内", description: "" },
        { label: "尚未确定", description: "" },
      ],
    },
  ],
};
const stageId = fixture.case.current_stage_id;
const assistant = {
  id: "assistant-question",
  role: "assistant",
  content: markdown,
  stage_id: stageId,
  task_id: null,
  created_at: "2026-11-01T00:00:01Z",
  citations: [],
  trace: [
    {
      id: "agent",
      label: "",
      detail: "",
      status: "completed",
      question_request: question,
    },
  ],
};

for (const width of [1440, 390]) {
  test(`answers Vercel questions and renders readable Markdown at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await page
      .context()
      .grantPermissions(["clipboard-read", "clipboard-write"]);
    const errors: string[] = [];
    const unexpected: string[] = [];
    const posted: Record<string, unknown>[] = [];
    let history = [assistant];
    page.on("pageerror", (error) => errors.push(error.message));
    // Complete API isolation: these browser scenarios cannot send model requests.
    await page.route("**/api/tea/**", async (route) => {
      const path = new URL(route.request().url()).pathname.replace(
        /^.*\/api\/tea/,
        "",
      );
      if (path === "/bootstrap")
        return route.fulfill({ json: fixture.bootstrap });
      if (path === "/documents")
        return route.fulfill({ json: { items: [], total: 0 } });
      if (path === `/cases/${fixture.case.id}`)
        return route.fulfill({ json: fixture.case });
      if (path.endsWith("/guide-conversation"))
        return route.fulfill({ json: fixture.guide });
      if (path.endsWith("/conversations"))
        return route.fulfill({ json: fixture.conversations });
      if (path.endsWith("/messages/stream")) {
        const payload = route.request().postDataJSON() as Record<
          string,
          unknown
        >;
        if (typeof payload.content !== "string")
          throw new Error("Expected user text");
        posted.push(payload);
        history = [
          {
            ...assistant,
            id: "assistant-answer",
            content: "已收到您的回答，接下来核对资料。",
            created_at: "2026-11-01T00:00:03Z",
            trace: [],
          },
        ];
        const done = {
          messages: [
            {
              ...assistant,
              id: "user-answer",
              role: "user",
              content: payload.content,
              created_at: "2026-11-01T00:00:02Z",
              trace: [],
            },
            ...history,
          ],
          case: fixture.case,
        };
        return route.fulfill({
          contentType: "text/event-stream",
          body: `event: delta\ndata: {"text":"已收到您的回答，接下来核对资料。"}\n\nevent: done\ndata: ${JSON.stringify(done)}\n\n`,
        });
      }
      if (path.endsWith("/messages"))
        return route.fulfill({
          json: { items: history, total: history.length },
        });
      unexpected.push(`${route.request().method()} ${path}`);
      return route.fulfill({
        status: 404,
        json: { error: { code: "unexpected_fixture_request", message: path } },
      });
    });
    await page.goto(entry);
    const destination = page.getByRole("radio", {
      name: "新加坡",
      exact: true,
    });
    await expect(destination).toBeEnabled();
    await expect(page.getByLabel("问题进度")).toHaveText("1 / 6");
    const answer = page.locator(".tea-answer").first();
    await expect(answer.locator("ol > li")).toHaveCount(2);
    await expect(answer.locator("ol ul > li")).toHaveCount(2);
    await expect(answer.locator("table tbody tr")).toHaveCount(2);
    const code = answer.locator('[data-streamdown="code-block"] pre');
    await expect(code).toBeVisible();
    await expect
      .poll(() => code.evaluate((node) => node.clientHeight))
      .toBeGreaterThan(1000);
    const layout = await code.evaluate((node) => ({
      height: node.clientHeight,
      scrollHeight: node.scrollHeight,
      width: node.clientWidth,
      scrollWidth: node.scrollWidth,
      lineHeight: getComputedStyle(node).lineHeight,
    }));
    expect(layout.height).toBeGreaterThan(1000);
    expect(layout.scrollHeight).toBeLessThanOrEqual(layout.height + 2);
    expect(layout.scrollWidth).toBeGreaterThan(layout.width);
    expect(
      await answer.evaluate((node) => getComputedStyle(node).lineHeight),
    ).toBe(width === 390 ? "26.6px" : "28.5px");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth + 1,
      ),
    ).toBe(false);
    const copy = answer.getByRole("button", { name: "复制代码" });
    await copy.click();
    await expect
      .poll(() => page.evaluate(() => navigator.clipboard.readText()))
      .toBe(markdown.split("```typescript\n")[1]!.split("```")[0]);
    const input = page.getByRole("textbox", { name: "向茶渡助手提问" });
    await input.fill("还没发送的草稿");
    await destination.click();
    await page.getByRole("button", { name: "下一题" }).click();
    await page.getByRole("checkbox", { name: "产品清单", exact: true }).click();
    await page.getByRole("checkbox", { name: "采购合同", exact: true }).click();
    await page
      .getByRole("textbox", { name: "其他情况或补充说明" })
      .fill("  暂时还没有报价  ");
    await page.getByRole("button", { name: "上一题" }).click();
    await expect(destination).toHaveAttribute("aria-checked", "true");
    await page.getByRole("button", { name: "下一题" }).click();
    await expect(
      page.getByRole("checkbox", { name: "产品清单", exact: true }),
    ).toHaveAttribute("aria-checked", "true");
    expect(posted).toHaveLength(0);
    await page.getByRole("button", { name: "下一题" }).click();
    await page.getByRole("radio", { name: "散茶", exact: true }).click();
    await page.getByRole("button", { name: "下一题" }).click();
    await page.getByRole("checkbox", { name: "散装", exact: true }).click();
    await page
      .getByRole("checkbox", { name: "零售小包装", exact: true })
      .click();
    await page.getByRole("button", { name: "下一题" }).click();
    await page.getByRole("radio", { name: "还在估算", exact: true }).click();
    await page.getByRole("button", { name: "下一题" }).click();
    await expect(page.getByLabel("问题进度")).toHaveText("6 / 6");
    await expect(page.getByRole("button", { name: "发送回答" })).toBeDisabled();
    await page
      .getByRole("textbox", { name: "其他情况或补充说明" })
      .fill("两个月以后");
    expect(posted).toHaveLength(0);
    await page.screenshot({
      path: test.info().outputPath(`choices-${width}.png`),
    });
    await page.getByRole("button", { name: "发送回答" }).click();
    await expect(
      page.getByText("已收到您的回答，接下来核对资料。", { exact: true }),
    ).toBeVisible();
    expect(posted).toHaveLength(1);
    expect(posted[0]).toMatchObject({
      stage_id: stageId,
      conversation_id: fixture.guide.id,
      content:
        "1、这批茶的出口目的地是哪里？\n新加坡\n\n2、已经准备了哪些资料？\n产品清单、采购合同\n暂时还没有报价\n\n3、产品是什么形态？\n散茶\n\n4、计划采用哪些包装？\n散装、零售小包装\n\n5、首批数量是否确定？\n还在估算\n\n6、预计何时发货？\n两个月以后",
    });
    await expect(input).toHaveValue("还没发送的草稿");
    await expect(page.getByRole("radio")).toHaveCount(0);
    expect(errors).toEqual([]);
    expect(unexpected).toEqual([]);
  });
}
