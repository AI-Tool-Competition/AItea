import { expect, it } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ClipboardSafeStreamdown } from "@/components/ai-elements/streamdown";
import { teaMarkdownPlugins } from "@/core/tea/markdown";

function render(content: string, streaming = false) {
  return renderToStaticMarkup(
    createElement(
      ClipboardSafeStreamdown,
      {
        ...teaMarkdownPlugins,
        mode: streaming ? "streaming" : "static",
        parseIncompleteMarkdown: streaming,
        animated: false,
        controls: false,
      },
      content,
    ),
  );
}

it("renders bare table line breaks while preserving Markdown emphasis and code", () => {
  for (const streaming of [false, true]) {
    const html = render(
      "| 事项 | 已确认 |\n| --- | --- |\n| **业务路线卡**<br>`task_id: route` | 熟茶<br/>B2B<BR />海外仓 |",
      streaming,
    );
    expect(html.match(/<br\s*\/?\s*>/g)).toHaveLength(3);
    expect(html).toContain('data-streamdown="strong">业务路线卡</span>');
    expect(html).toContain("task_id: route</code>");
    expect(html).not.toContain("&lt;br");
  }
});

it("keeps escaped breaks and code examples literal and refuses HTML attributes", () => {
  const html = render(
    [
      "| 示例 | 说明 |",
      "| --- | --- |",
      '| `<br>` | &lt;br&gt;<br class="unsafe">继续 |',
      '| 图片 | <img src="https://example.test/x" onerror="alert(1)"> |',
      '| 脚本 | <script>alert(1)</script><iframe src="https://example.test"></iframe> |',
    ].join("\n"),
  );
  expect(html).toContain("&lt;br&gt;</code>");
  expect(html).toContain("&lt;br&gt;");
  expect(html).not.toMatch(/<br(?:\s|\/?>)/);
  expect(html).not.toMatch(/<(?:img|script|iframe)(?:\s|>)/);
  expect(html).not.toContain('class="unsafe"');
});

it("leaves prose and fenced code unchanged when they contain break tags", () => {
  const html = render("正文中的 <br> 示例。\n\n```html\n<br>\n``` ");
  expect(html).toContain("&lt;br&gt;");
  expect(html).not.toMatch(/<br\s*\/?\s*>/);
});
