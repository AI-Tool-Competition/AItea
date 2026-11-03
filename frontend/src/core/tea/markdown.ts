import type { Root } from "mdast";
import { visit } from "unist-util-visit";

import { streamdownPluginsWithoutRawHtml } from "@/core/streamdown";

/** Recognize bare table line breaks without enabling model-authored HTML. */
function remarkTeaTableBreaks() {
  return (tree: Root) => {
    visit(tree, "tableCell", (cell) => {
      visit(cell, "html", (node, index, parent) => {
        if (
          /^<br\s*\/?>$/i.test(node.value.trim()) &&
          index !== undefined &&
          parent
        ) {
          parent.children[index] = {
            type: "break",
          };
        }
      });
    });
  };
}

export const teaMarkdownPlugins = {
  ...streamdownPluginsWithoutRawHtml,
  // Streamdown turns raw HTML into text before rehype when rehypeRaw is off.
  // Recognize only bare table breaks before that step; retain the shared
  // sanitizer and keep all other HTML and escaped code samples inert.
  remarkPlugins: [
    ...(streamdownPluginsWithoutRawHtml.remarkPlugins ?? []),
    remarkTeaTableBreaks,
  ],
};
