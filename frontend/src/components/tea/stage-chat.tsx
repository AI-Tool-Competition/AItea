"use client";

import {
  ArrowUp,
  BookOpen,
  Circle,
  Leaf,
  Paperclip,
  RotateCcw,
  Square,
  X,
} from "lucide-react";
import { memo, useLayoutEffect, useRef } from "react";
import type { StickToBottomContext } from "use-stick-to-bottom";

import {
  Conversation,
  ConversationContent,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { ClipboardSafeStreamdown } from "@/components/ai-elements/streamdown";
import { Button } from "@/components/ui/button";
import { streamdownPluginsWithoutRawHtml } from "@/core/streamdown";
import type {
  Capabilities,
  CaseDetail,
  Message,
  StageState,
} from "@/core/tea/types";
import { type ChatTurn, useStageChat } from "@/core/tea/use-stage-chat";

import { CitationCard } from "./library";
import { Busy, dateLabel, ErrorNotice, Trace } from "./shared";
import { StageAnalysisPanel } from "./stage-analysis";

const Answer = memo(function Answer({
  content,
  streaming = false,
}: {
  content: string;
  streaming?: boolean;
}) {
  return (
    <ClipboardSafeStreamdown
      {...streamdownPluginsWithoutRawHtml}
      mode={streaming ? "streaming" : "static"}
      isAnimating={streaming}
      parseIncompleteMarkdown={streaming}
    >
      {content}
    </ClipboardSafeStreamdown>
  );
});

const SavedMessage = memo(function SavedMessage({
  message,
}: {
  message: Message;
}) {
  return (
    <article className={`tea-message ${message.role}`}>
      {message.role === "assistant" && (
        <div className="tea-message-avatar" aria-hidden="true">
          <Leaf size={16} strokeWidth={1.6} />
        </div>
      )}
      <div className="tea-message-body">
        <div
          className="tea-message-label"
          title={dateLabel(message.created_at)}
        >
          {message.role === "assistant" ? "茶渡助手" : "你"}
        </div>
        <div className="tea-message-content">
          {message.role === "user" ? (
            message.content
          ) : (
            <Answer content={message.content} />
          )}
        </div>
        {message.trace?.length > 0 && <Trace steps={message.trace} />}
        {message.citations?.length > 0 && (
          <details className="tea-message-citations">
            <summary>
              <BookOpen size={13} />
              查看 {message.citations.length} 项引用依据
            </summary>
            {message.citations.map((citation, index) => (
              <CitationCard
                key={`${citation.document_id}-${index}`}
                citation={citation}
              />
            ))}
          </details>
        )}
      </div>
    </article>
  );
});

function PendingTurn({
  turn,
  busy,
  onRetry,
}: {
  turn: ChatTurn;
  busy: boolean;
  onRetry: () => void;
}) {
  return (
    <>
      <article className="tea-message user">
        <div className="tea-message-body">
          <div className="tea-message-label">你</div>
          <div className="tea-message-content">{turn.request.content}</div>
        </div>
      </article>
      <article className="tea-message assistant" aria-label="茶渡助手回复">
        <div className="tea-message-avatar" aria-hidden="true">
          <Leaf size={16} strokeWidth={1.6} />
        </div>
        <div className="tea-message-body">
          <div className="tea-message-label">茶渡助手</div>
          {turn.text && (
            <div className="tea-message-content">
              <Answer
                content={turn.text}
                streaming={turn.state === "sending"}
              />
            </div>
          )}
          {turn.state === "sending" ? (
            <div className="tea-assistant-working" role="status">
              <span>{turn.text ? "正在回复…" : turn.label}</span>
              <i />
              <i />
              <i />
            </div>
          ) : (
            <div className="tea-turn-feedback">
              <span role={turn.error ? "alert" : "status"}>
                {turn.error ?? "已停止回复，以上内容尚未完成。"}
              </span>
              <Button
                variant="ghost"
                size="sm"
                disabled={busy}
                onClick={onRetry}
              >
                <RotateCcw size={14} />
                重试回复
              </Button>
            </div>
          )}
        </div>
      </article>
    </>
  );
}

export function StageChat({
  record,
  stage,
  accountId,
  taskId,
  onTask,
  onUpdate,
  onUpload,
  capabilities,
}: {
  record: CaseDetail;
  stage: StageState;
  accountId: string;
  taskId: string;
  onTask: (id: string) => void;
  onUpdate: (record: CaseDetail) => void;
  onUpload: () => void;
  capabilities: Capabilities;
}) {
  const chat = useStageChat({
    accountId,
    caseId: record.id,
    stageId: stage.id,
    onUpdate,
  });
  const input = useRef<HTMLTextAreaElement>(null);
  const conversation = useRef<StickToBottomContext>(null);
  const followMessages = useRef(false);
  const focusedTask = stage.tasks.find((task) => task.id === taskId);
  const hasConversation = chat.messages.length > 0 || chat.turns.length > 0;
  const sendingId = chat.turns.find((turn) => turn.state === "sending")?.request
    .client_message_id;
  useLayoutEffect(() => {
    if (sendingId) {
      followMessages.current = true;
      void conversation.current?.scrollToBottom("instant");
    }
  }, [sendingId]);
  useLayoutEffect(() => {
    if (!input.current) return;
    input.current.style.height = "auto";
    input.current.style.height = `${Math.min(input.current.scrollHeight, 200)}px`;
  }, [chat.text]);

  function releaseEntryPosition() {
    if (followMessages.current) return;
    followMessages.current = true;
    conversation.current?.stopScroll();
  }
  return (
    <div
      className="tea-chat"
      onPointerDownCapture={releaseEntryPosition}
      onWheelCapture={releaseEntryPosition}
      onTouchMoveCapture={releaseEntryPosition}
      onKeyDownCapture={releaseEntryPosition}
    >
      <Conversation
        contextRef={conversation}
        initial={false}
        resize="instant"
        targetScrollTop={(target) => (followMessages.current ? target : 0)}
        className="tea-conversation"
        aria-label="与茶渡助手的对话"
      >
        <ConversationContent className="tea-conversation-content">
          <StageAnalysisPanel
            accountId={accountId}
            record={record}
            stage={stage}
            onUpdate={onUpdate}
          />
          {chat.hasEarlier && (
            <Button
              variant="ghost"
              size="sm"
              className="tea-earlier-messages"
              disabled={chat.moreBusy || chat.sending}
              onClick={() => void chat.loadEarlier()}
            >
              {chat.moreBusy ? "正在读取…" : "加载更早对话"}
            </Button>
          )}
          {chat.loading ? (
            <Busy label="读取对话记录…" />
          ) : (
            <>
              {chat.timeline.map((entry) =>
                entry.message ? (
                  <SavedMessage
                    key={entry.message.id}
                    message={entry.message}
                  />
                ) : (
                  <PendingTurn
                    key={entry.turn.request.client_message_id}
                    turn={entry.turn}
                    busy={chat.sending || chat.loading || !!chat.historyError}
                    onRetry={() => chat.retry(entry.turn)}
                  />
                ),
              )}
              {!hasConversation && !chat.historyError && (
                <div className="tea-chat-welcome">
                  <h2>继续聊聊这个节点</h2>
                  <p>
                    {stage.status === "locked"
                      ? "可以先了解这一阶段的准备要求，完成前一阶段后继续办理。"
                      : "可以接着上面的分析提问，或上传资料一起核对。"}
                  </p>
                  <div className="tea-chat-suggestions">
                    {[
                      "这个阶段需要准备哪些资料？",
                      "帮我看看还缺什么",
                      "下一步应该怎么做？",
                    ].map((question) => (
                      <button
                        key={question}
                        onClick={() => {
                          chat.edit(question);
                          input.current?.focus();
                        }}
                      >
                        {question}
                        <ArrowUp size={13} />
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </ConversationContent>
        <ConversationScrollButton
          aria-label="滚动到最新消息"
          className="tea-scroll-bottom"
        />
      </Conversation>
      <div className="tea-composer-area">
        {chat.historyError && (
          <ErrorNotice message={chat.historyError} onRetry={chat.reload} />
        )}
        {focusedTask && (
          <div className="tea-current-task">
            <Circle size={10} />
            <span>正在讨论：{focusedTask.title}</span>
            <button onClick={() => onTask("")} aria-label="取消任务聚焦">
              <X size={14} />
            </button>
          </div>
        )}
        <form
          className="tea-composer"
          onSubmit={(event) => {
            event.preventDefault();
            chat.send(taskId);
            input.current?.focus();
          }}
        >
          <textarea
            ref={input}
            aria-label="向茶渡助手提问"
            aria-describedby="tea-composer-hint"
            value={chat.text}
            maxLength={12000}
            placeholder={
              chat.sending
                ? "可以继续写下一条，回复结束后发送…"
                : "向茶渡助手提问…"
            }
            rows={1}
            onChange={(event) => chat.edit(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing &&
                event.keyCode !== 229
              ) {
                event.preventDefault();
                chat.send(taskId);
              }
            }}
          />
          <div className="tea-composer-toolbar">
            <Button
              variant="ghost"
              size="icon"
              type="button"
              onClick={onUpload}
              aria-label="上传资料"
              title="上传资料"
            >
              <Paperclip size={18} />
            </Button>
            <span className="tea-composer-shortcut" id="tea-composer-hint">
              Enter 发送 · Shift + Enter 换行
            </span>
            {chat.sending ? (
              <button
                className="tea-send"
                type="button"
                onClick={chat.stop}
                aria-label="停止回复"
                title="停止回复"
              >
                <Square size={15} fill="currentColor" />
              </button>
            ) : (
              <button
                className="tea-send"
                type="submit"
                aria-label="发送消息"
                title="发送消息"
                disabled={
                  !chat.text.trim() || chat.loading || !!chat.historyError
                }
              >
                <ArrowUp size={19} />
              </button>
            )}
          </div>
        </form>
        <p className="tea-composer-note">
          {capabilities.assistant_mode === "demo"
            ? "演示助手 · 基于资料与配置规则"
            : "AI 助手 · 请结合引用原文核对办理要求"}
        </p>
      </div>
    </div>
  );
}
