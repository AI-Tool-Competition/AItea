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
  ClipboardSafeStreamdown,
} from "@/components/ai-elements";
import { Button } from "@/components/ui/button";
import { streamdownPluginsWithoutRawHtml } from "@/core/streamdown";
import type {
  Capabilities,
  CaseDetail,
  Message,
  StageState,
  TraceStep,
} from "@/core/tea/types";
import { type ChatTurn, useStageChat } from "@/core/tea/use-stage-chat";

import { LegalResultView } from "./legal-result";
import { CitationCard } from "./library";
import { Busy, dateLabel, ErrorNotice } from "./shared";
import { MessageActivity } from "./tool-activity";

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
      animated={false}
      caret="circle"
      linkSafety={{ enabled: false }}
      aria-busy={streaming}
      parseIncompleteMarkdown={streaming}
    >
      {content}
    </ClipboardSafeStreamdown>
  );
});

const emptyTrace: TraceStep[] = [];

const ChatMessage = memo(function ChatMessage({
  role,
  message,
  turn,
  busy = false,
  onRetry,
}: {
  role: Message["role"];
  message?: Message;
  turn?: ChatTurn;
  busy?: boolean;
  onRetry?: () => void;
}) {
  const sending = turn?.state === "sending";
  const delivery = (message?.trace ?? turn?.steps ?? emptyTrace).find(
    (step) => step.delivery_status,
  )?.delivery_status;
  const deliveryLabels = {
    answered: "",
    partially_answered: "部分完成 · 请查看尚未确认的事项",
    needs_user_input: "需要补充信息",
    execution_failed: "执行未完成 · 请查看具体原因后重试",
    unsupported_jurisdiction: "当前专业资料能力尚未覆盖所问法域",
  };
  const content =
    message?.content ??
    (role === "user" ? turn?.request.content : turn?.text) ??
    "";
  return (
    <article
      className={`tea-message ${role}`}
      aria-label={role === "assistant" ? "茶渡助手回复" : undefined}
    >
      {role === "assistant" && (
        <div className="tea-message-avatar" aria-hidden="true">
          <Leaf size={16} strokeWidth={1.6} />
        </div>
      )}
      <div className="tea-message-body">
        <div
          className="tea-message-label"
          title={message ? dateLabel(message.created_at) : undefined}
        >
          {role === "assistant" ? "茶渡助手" : "你"}
        </div>
        {role === "assistant" && (
          <MessageActivity
            steps={message?.trace ?? turn?.steps ?? emptyTrace}
            running={sending}
            answering={Boolean(content)}
            incomplete={turn?.state === "failed"}
            stopped={turn?.state === "stopped"}
          />
        )}
        <div className="tea-message-content">
          {role === "assistant" &&
            !sending &&
            delivery &&
            deliveryLabels[delivery] && (
              <p className="tea-small-note" role="status">
                {deliveryLabels[delivery]}
              </p>
            )}
          {role === "user" ? (
            content
          ) : message?.guide_result &&
            !message.trace.some((step) => step.id === "agent") ? (
            <div className="tea-chat-report">
              <h3>分析结论与下一步</h3>
              <p className="tea-small-note">
                {message.guide_result.assistant_mode === "demo"
                  ? "演示模式 · 未调用大模型"
                  : "结合当前情况与资料整理"}
              </p>
              <LegalResultView result={message.guide_result} compact />
              <details className="tea-chat-full-report">
                <summary>完整报告与原文依据</summary>
                <LegalResultView
                  result={message.guide_result}
                  showTrace={false}
                />
              </details>
            </div>
          ) : content ? (
            <Answer content={content} streaming={sending} />
          ) : null}
        </div>
        {!!message?.citations?.length && (
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
        {role === "assistant" &&
          message &&
          delivery === "execution_failed" &&
          onRetry && (
            <Button variant="ghost" size="sm" disabled={busy} onClick={onRetry}>
              <RotateCcw size={14} />
              重试回复
            </Button>
          )}
        {role === "assistant" && turn && !sending && (
          <div className="tea-turn-feedback">
            <span role={turn.error ? "alert" : "status"}>
              {turn.error ?? "已停止回复，以上内容尚未完成。"}
            </span>
            <Button variant="ghost" size="sm" disabled={busy} onClick={onRetry}>
              <RotateCcw size={14} />
              重试回复
            </Button>
          </div>
        )}
      </div>
    </article>
  );
});

export function StageChat({
  record,
  stage,
  accountId,
  taskId,
  onTask,
  onUpdate,
  onUpload,
  capabilities,
  conversationId,
  guide = false,
}: {
  record: CaseDetail;
  stage: StageState;
  accountId: string;
  taskId: string;
  onTask: (id: string) => void;
  onUpdate: (record: CaseDetail) => void;
  onUpload: () => void;
  capabilities: Capabilities;
  conversationId?: string;
  guide?: boolean;
}) {
  const chat = useStageChat({
    accountId,
    caseId: record.id,
    stageId: stage.id,
    conversationId,
    onUpdate,
  });
  const input = useRef<HTMLTextAreaElement>(null);
  const conversation = useRef<StickToBottomContext>(null);
  const followMessages = useRef(true);
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
              {chat.timeline.flatMap((entry) =>
                entry.message ? (
                  <ChatMessage
                    key={chat.messageKey(entry.message)}
                    role={entry.message.role}
                    message={entry.message}
                    busy={chat.sending || chat.loading || !!chat.historyError}
                    onRetry={
                      chat.messages[
                        chat.messages.findIndex(
                          (item) => item.id === entry.message.id,
                        ) - 1
                      ]?.role === "user"
                        ? () => chat.retryMessage(entry.message)
                        : undefined
                    }
                  />
                ) : (
                  [
                    <ChatMessage
                      key={`${entry.turn.request.client_message_id}:user`}
                      role="user"
                      turn={entry.turn}
                    />,
                    <ChatMessage
                      key={`${entry.turn.request.client_message_id}:assistant`}
                      role="assistant"
                      turn={entry.turn}
                      busy={chat.sending || chat.loading || !!chat.historyError}
                      onRetry={() => chat.retry(entry.turn)}
                    />,
                  ]
                ),
              )}
              {!hasConversation && !chat.historyError && (
                <div className="tea-chat-welcome">
                  <h2>{guide ? "一起梳理这个节点" : "新的会话"}</h2>
                  <p>
                    {stage.status === "locked"
                      ? "可以先了解这一阶段的准备要求，完成前一阶段后继续办理。"
                      : "直接说说你想办理的事情，也可以上传资料，一起梳理下一步。"}
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
        <div className="tea-composer-content">
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
              <div className="tea-composer-tools">
                <Button
                  className="tea-composer-upload"
                  variant="ghost"
                  size="icon"
                  type="button"
                  onClick={onUpload}
                  aria-label="上传资料"
                  title="上传资料"
                >
                  <Paperclip size={18} />
                </Button>
              </div>
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
          {capabilities.assistant_mode === "demo" && (
            <p className="tea-composer-note">演示助手 · 基于资料与配置规则</p>
          )}
        </div>
      </div>
    </div>
  );
}
