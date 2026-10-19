"use client";

import { ArrowDownToLine } from "lucide-react";
import { Fragment, useEffect, useRef, useState } from "react";

import { ClipboardSafeStreamdown } from "@/components/ai-elements/streamdown";
import { Button } from "@/components/ui/button";
import { streamdownPluginsWithoutRawHtml } from "@/core/streamdown/plugins";
import {
  documentImageUrl,
  downloadUrl,
  errorMessage,
  safeSourceUrl,
  teaRequest,
} from "@/core/tea/api";
import { documentSize, documentState } from "@/core/tea/document-state";
import {
  type Document,
  type DocumentDetail,
  type DocumentPreviewBlock,
} from "@/core/tea/types";

import { Busy, Empty, ErrorNotice } from "./shared";

const PAGE_SIZE = 100;

// A single reading surface shared by the business library and the law library.
export function DocumentPreview({
  document,
  sample,
  processing,
}: {
  document: Document;
  sample?: DocumentDetail;
  processing?: { error: string; retry: () => void };
}) {
  const [page, setPage] = useState<{ offset: number; data: DocumentDetail }>();
  const [request, setRequest] = useState({ offset: 0, attempt: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const reading = useRef<HTMLDivElement>(null);
  const previousOffset = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!page) return;
    if (
      previousOffset.current !== undefined &&
      previousOffset.current !== page.offset
    )
      reading.current?.scrollIntoView?.({ block: "start" });
    previousOffset.current = page.offset;
  }, [page]);
  useEffect(() => {
    if (sample) {
      setPage({ offset: 0, data: sample });
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    teaRequest<DocumentDetail>(
      `/documents/${encodeURIComponent(document.id)}?element_offset=${request.offset}&chunk_offset=0&limit=${PAGE_SIZE}`,
      { signal: controller.signal },
    )
      .then((data) => {
        if (!controller.signal.aborted)
          setPage({ offset: request.offset, data });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [document.id, document.status, document.chunk_count, request, sample]);
  const blocks =
    page?.data.preview?.blocks ??
    page?.data.elements.map((part) => ({
      kind: part.kind ?? "paragraph",
      text: part.content_text ?? part.text ?? "",
    }));
  const total = page?.data.preview?.total ?? page?.data.element_total ?? 0;
  const summary = page?.data.preview?.summary;
  const currentDocument =
    document.status === "processing" &&
    page?.data.document.status !== "processing"
      ? (page?.data.document ?? document)
      : document;
  const visibleError = error ? error : processing?.error;
  return (
    <div className="tea-document-preview">
      <div className="tea-preview-toolbar">
        <span>
          {document.filename.split(".").at(-1)?.toUpperCase()} ·{" "}
          {documentSize(document.size)}
        </span>
        <span
          className={`tea-preview-status ${documentState(currentDocument).tone}`}
          role="status"
        >
          {documentState(currentDocument).label}
        </span>
        {!sample && (
          <Button asChild size="sm" variant="ghost">
            <a href={downloadUrl(document.id)}>
              <ArrowDownToLine size={14} />
              下载原件
            </a>
          </Button>
        )}
      </div>
      {visibleError && (
        <ErrorNotice
          message={visibleError}
          onRetry={() =>
            error
              ? setRequest((current) => ({
                  ...current,
                  attempt: current.attempt + 1,
                }))
              : processing?.retry()
          }
        />
      )}
      {loading && <Busy label="读取正文…" />}
      {page && (
        <div
          ref={reading}
          className="tea-preview-reading"
          aria-busy={loading}
          key={page.offset}
        >
          {summary && page.offset === 0 && (
            <div className="tea-preview-summary">
              <h3>摘要</h3>
              <p>{summary}</p>
            </div>
          )}
          {!!blocks?.length && (
            <article className="tea-preview-content" aria-label="资料正文">
              <PreviewBlocks blocks={blocks} documentId={document.id} />
            </article>
          )}
          {!blocks?.length && (
            <Empty
              title={
                currentDocument.status === "processing"
                  ? "正在处理正文"
                  : currentDocument.status === "failed"
                    ? "正文解析失败"
                    : "暂无解析内容"
              }
            >
              可下载原始文件查看。
            </Empty>
          )}
        </div>
      )}
      {page && total > PAGE_SIZE && (
        <footer className="tea-preview-pagination">
          <span>
            第 {page.offset + 1}–{page.offset + (blocks?.length ?? 0)} 段 / 共{" "}
            {total} 段
          </span>
          <div>
            <Button
              variant="ghost"
              size="sm"
              disabled={loading || page.offset === 0}
              onClick={() =>
                setRequest({
                  offset: Math.max(0, page.offset - PAGE_SIZE),
                  attempt: 0,
                })
              }
            >
              上一页正文
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={loading || page.offset + PAGE_SIZE >= total}
              onClick={() =>
                setRequest({ offset: page.offset + PAGE_SIZE, attempt: 0 })
              }
            >
              下一页正文
            </Button>
          </div>
        </footer>
      )}
    </div>
  );
}

function PreviewBlocks({
  blocks,
  documentId,
}: {
  blocks: DocumentPreviewBlock[];
  documentId: string;
}) {
  // Render consecutive text as one Markdown document to preserve lists, tables
  // and formulas instead of surrounding every paragraph with a card.
  const runs: (string | DocumentPreviewBlock)[] = [];
  let text: string[] = [];
  const flush = () => {
    if (text.length) runs.push(text.join("\n\n"));
    text = [];
  };
  for (const block of blocks) {
    if (block.kind === "image" || block.cells) {
      flush();
      runs.push(block);
      continue;
    }
    const value = block.text?.trim();
    if (!value) continue;
    text.push(
      block.kind === "heading" || block.kind === "title"
        ? `## ${value.replace(/^#+\s*/, "")}`
        : block.kind === "list" && !/^\s*(?:[-*+] |\d+[.)] )/.test(value)
          ? value
              .split("\n")
              .map((line) => `- ${line}`)
              .join("\n")
          : block.kind === "formula"
            ? `$$\n${value}\n$$`
            : block.kind === "code"
              ? `\`\`\`\n${value}\n\`\`\``
              : value,
    );
  }
  flush();
  return runs.map((run, i) =>
    typeof run === "string" ? (
      <ClipboardSafeStreamdown
        key={i}
        {...streamdownPluginsWithoutRawHtml}
        mode="static"
        controls={false}
        components={{
          // Only authenticated, mapped image assets are displayed below. Markdown
          // from uploaded files cannot make the browser fetch arbitrary image URLs.
          img: ({ alt }) => (alt ? <span>{alt}</span> : null),
          a: ({ href, children }) => {
            const url = safeSourceUrl(href);
            return url ? (
              <a href={url} target="_blank" rel="noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            );
          },
        }}
      >
        {run}
      </ClipboardSafeStreamdown>
    ) : run.kind === "image" ? (
      <PreviewImage
        key={run.image_id ?? i}
        block={run}
        documentId={documentId}
      />
    ) : (
      <div className="tea-preview-table" key={i}>
        <table>
          <tbody>
            {Array.from(new Set(run.cells?.map((cell) => cell.row))).map(
              (row) => (
                <tr key={row}>
                  {run.cells
                    ?.filter((cell) => cell.row === row)
                    .map((cell, index) => (
                      <td
                        key={index}
                        colSpan={cell.col_span}
                        rowSpan={cell.row_span}
                      >
                        {cell.text}
                      </td>
                    ))}
                </tr>
              ),
            )}
          </tbody>
        </table>
      </div>
    ),
  );
}

function PreviewImage({
  block,
  documentId,
}: {
  block: DocumentPreviewBlock;
  documentId: string;
}) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  if (!block.image_id) return null;
  const hasCaption =
    Boolean(block.caption?.trim()) || Boolean(block.summary?.trim());
  return (
    <figure className="tea-preview-figure">
      {failed ? (
        <div className="tea-preview-image-error">
          <span>图片读取失败</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setFailed(false);
              setAttempt((value) => value + 1);
            }}
          >
            重试图片
          </Button>
        </div>
      ) : (
        <Fragment key={attempt}>
          {/* Authenticated raster image, served with its document's access boundary. */}
          <img
            src={documentImageUrl(documentId, block.image_id)}
            alt={block.caption?.trim() ? block.caption : "资料图片"}
            width={block.width}
            height={block.height}
            loading="lazy"
            onError={() => setFailed(true)}
          />
        </Fragment>
      )}
      {hasCaption && (
        <figcaption>
          {block.caption && <span>{block.caption}</span>}
          {block.summary && <p>{block.summary}</p>}
        </figcaption>
      )}
    </figure>
  );
}
