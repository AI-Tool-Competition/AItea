/**
 * Stable public surface for the AI interaction primitives used by product
 * features. Keep feature components importing from here so implementation
 * files can evolve without spreading path-level coupling.
 */
export {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "./conversation";
export type {
  ConversationEmptyStateProps,
  ConversationProps,
  ConversationContentProps,
  ConversationScrollButtonProps,
} from "./conversation";

export {
  ChainOfThought,
  ChainOfThoughtContent,
  ChainOfThoughtHeader,
  ChainOfThoughtStep,
} from "./chain-of-thought";
export type {
  ChainOfThoughtProps,
  ChainOfThoughtContentProps,
  ChainOfThoughtHeaderProps,
  ChainOfThoughtStepProps,
} from "./chain-of-thought";

export { Task, TaskContent, TaskItem, TaskTrigger } from "./task";
export type { TaskProps, TaskContentProps, TaskItemProps, TaskTriggerProps } from "./task";

export { ClipboardSafeStreamdown } from "./streamdown";
export type { ClipboardSafeStreamdownProps } from "./streamdown";
