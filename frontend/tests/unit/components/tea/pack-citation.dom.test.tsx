import { afterEach, expect, test } from "@rstest/core";
import { cleanup, render, screen } from "@testing-library/react";

import { CitationCard } from "@/components/tea/library";
import type { Citation } from "@/core/tea/types";

afterEach(cleanup);

test("web sources display the website address without an archive download or review gate", () => {
  const citation: Citation = {
    title: "行业协会",
    excerpt: "公开的会员和行业信息。",
    source_url: "http://association.example/",
    metadata: { content_role: "search_snippet" },
  };
  render(<CitationCard citation={citation} />);
  expect(
    screen
      .getByRole("link", { name: "http://association.example/" })
      .getAttribute("href"),
  ).toBe("http://association.example/");
  expect(screen.queryByRole("link", { name: "原始文件" })).toBeNull();
  expect(screen.getByText("网页信息")).toBeTruthy();
  expect(screen.queryByText(/待审核|生效日期/)).toBeNull();
});

test("pack evidence displays its article and official source without a nonexistent archive download", () => {
  const citation: Citation = {
    document_id: "pack-ID-doc",
    document_version: 12345,
    title: "标签规定",
    excerpt: "来源中保存的条文片段。",
    locator: { article: "21" },
    source_url: "https://example.gov/law",
    metadata: {
      kind: "law",
      jurisdiction: "ID",
      language: "id",
      published_at: null,
      effective_from: null,
      effective_to: null,
      review_status: "draft",
      evidence_store: "country_pack",
      source_type: "法规原文",
    },
  };
  render(<CitationCard citation={citation} />);
  expect(
    screen.getByRole("link", { name: "来源网站" }).getAttribute("href"),
  ).toBe("https://example.gov/law");
  expect(screen.queryByRole("link", { name: "原始文件" })).toBeNull();
  expect(screen.getByText(/条款 21/)).toBeTruthy();
  expect(screen.getByText("来源中保存的条文片段。")).toBeTruthy();
});
