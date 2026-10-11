"use client";

import {
  Background,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import {
  Check,
  FileSearch,
  Focus,
  ListChecks,
  Minus,
  Plus,
  Sparkles,
} from "lucide-react";
import { useMemo } from "react";

import { type TraceStep } from "@/core/tea/types";

export type ConfigurationNode = {
  id: string;
  label: string;
  subtitle: string;
  kind: "retrieve" | "check" | "guide" | "draft" | "verify";
  enabled?: boolean;
};
type CanvasNode = Node<
  ConfigurationNode & {
    index: number;
    selected: boolean;
    trace?: TraceStep;
    onSelect: (id: string) => void;
  },
  "configuration"
>;
const icons = {
  retrieve: FileSearch,
  check: ListChecks,
  guide: Sparkles,
  draft: ListChecks,
  verify: ListChecks,
};

function ConfigurationCard({ data }: NodeProps<CanvasNode>) {
  const Icon = icons[data.kind];
  return (
    <div
      className={`ta-node ${data.selected ? "is-selected" : ""} ${data.enabled === false ? "is-disabled" : ""} ${data.trace?.status ?? ""}`}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <button
        className="nodrag nopan ta-node-button"
        data-inspector-trigger
        onClick={() => data.onSelect(data.id)}
        aria-pressed={data.selected}
        aria-label={`编辑${data.label}`}
      >
        <div className="ta-node-kicker">
          <span>
            <Icon size={15} />
            分析步骤
          </span>
          <span>{String(data.index + 1).padStart(2, "0")}</span>
        </div>
        <strong>{data.label || "未命名节点"}</strong>
        <p>{data.subtitle}</p>
        <footer>
          {data.trace ? (
            <>
              <Check size={12} />
              {data.trace.status === "completed"
                ? "已执行"
                : data.trace.status === "failed"
                  ? "执行失败"
                  : "已跳过"}
            </>
          ) : (
            <>
              <span className="ta-dot" />
              {data.enabled === false ? "已停用" : "已启用"}
            </>
          )}
        </footer>
      </button>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}
const nodeTypes = { configuration: ConfigurationCard };
function CanvasControls() {
  const flow = useReactFlow();
  return (
    <Panel position="bottom-left" className="ta-canvas-controls">
      <button onClick={() => void flow.zoomOut()} aria-label="缩小画布">
        <Minus size={16} />
      </button>
      <button
        onClick={() => void flow.fitView({ padding: 0.18, maxZoom: 1 })}
        aria-label="显示完整流程"
      >
        <Focus size={16} />
      </button>
      <button onClick={() => void flow.zoomIn()} aria-label="放大画布">
        <Plus size={16} />
      </button>
    </Panel>
  );
}
export function ConfigurationCanvas({
  items,
  selected,
  onSelect,
  trace,
  label,
}: {
  items: ConfigurationNode[];
  selected: string;
  onSelect: (id: string) => void;
  trace?: TraceStep[];
  label: string;
}) {
  const nodes = useMemo<CanvasNode[]>(
    () =>
      items.map((item, index) => ({
        id: item.id,
        type: "configuration",
        position: { x: index * 296, y: 0 },
        data: {
          ...item,
          index,
          selected: item.id === selected,
          onSelect,
          trace: trace?.find((step) => step.id === item.id),
        },
        draggable: false,
        selectable: false,
        ariaLabel: item.label,
      })),
    [items, selected, onSelect, trace],
  );
  const edges = useMemo<Edge[]>(
    () =>
      items.slice(1).map((item, index) => ({
        id: `${items[index]!.id}-${item.id}`,
        source: items[index]!.id,
        target: item.id,
        type: "smoothstep",
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: "#9aa99f",
          width: 15,
          height: 15,
        },
        style: { stroke: "#9aa99f", strokeWidth: 1.5 },
      })),
    [items],
  );
  return (
    <section className="ta-canvas-section" aria-label={label}>
      <div className="ta-canvas-caption">
        <span>
          <span className="ta-dot" />
          {label}
        </span>
        <small>点击节点编辑 · 按箭头顺序执行</small>
      </div>
      <div className="ta-canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          defaultViewport={{ x: 30, y: 110, zoom: 0.9 }}
          minZoom={0.15}
          maxZoom={1.5}
          nodesDraggable={false}
          nodesConnectable={false}
          edgesFocusable={false}
          deleteKeyCode={null}
          zoomOnScroll={false}
          preventScrolling={false}
        >
          <Background color="#cfd8d0" gap={18} size={1} />
          <CanvasControls />
        </ReactFlow>
      </div>
    </section>
  );
}
