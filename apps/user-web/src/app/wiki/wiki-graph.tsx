"use client";

import {
  Background,
  ControlButton,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type MiniMapNodeProps,
  type Node,
  type NodeProps,
  type ReactFlowInstance
} from "@xyflow/react";
import type { WikiGraph, WikiGraphNode } from "@wiki-agent/web-ui";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties
} from "react";
import { GRAPH_KINDS, graphFocusPaths, graphNodeSize, layoutWikiGraph } from "./graph-layout";

const KIND_LABELS: Record<WikiGraphNode["kind"], string> = {
  source: "来源",
  entity: "实体",
  concept: "概念",
  pending: "待处理"
};

type WikiNodeData = {
  [key: string]: unknown;
  path: string;
  title: string;
  label: string;
  kind: WikiGraphNode["kind"];
  active: boolean;
  degree: number;
  muted: boolean;
  highlighted: boolean;
  diameter: number;
  graphWidth: number;
  onOpen: (path: string) => void;
  onFocus: (path: string) => void;
  onBlur: () => void;
};
type WikiFlowNode = Node<WikiNodeData, "wiki">;

function WikiMiniMapNode({
  id, x, y, height, color, strokeColor, strokeWidth, className, style, onClick
}: MiniMapNodeProps) {
  return (
    <circle
      cx={x + height / 2}
      cy={y + height / 2}
      r="1.4%"
      className={`react-flow__minimap-node ${className}`}
      onClick={(event) => onClick?.(event, id)}
      shapeRendering="geometricPrecision"
      style={{ ...style, fill: color, stroke: strokeColor, strokeWidth }}
      vectorEffect="non-scaling-stroke"
    />
  );
}

const GRAPH_PORTS = [
  { name: "right", angle: 0, position: Position.Right },
  { name: "bottom-right", angle: Math.PI / 4, position: Position.Bottom },
  { name: "bottom", angle: Math.PI / 2, position: Position.Bottom },
  { name: "bottom-left", angle: 3 * Math.PI / 4, position: Position.Bottom },
  { name: "left", angle: Math.PI, position: Position.Left },
  { name: "top-left", angle: -3 * Math.PI / 4, position: Position.Top },
  { name: "top", angle: -Math.PI / 2, position: Position.Top },
  { name: "top-right", angle: -Math.PI / 4, position: Position.Top }
] as const;

function getGraphPort(angle: number): string {
  const sector = (Math.round(angle / (Math.PI / 4)) + GRAPH_PORTS.length) % GRAPH_PORTS.length;
  return GRAPH_PORTS[sector]!.name;
}

function WikiNode({ data }: NodeProps<WikiFlowNode>) {
  return (
    <>
      {GRAPH_PORTS.flatMap((port) => {
        const radius = data.diameter / 2;
        const style = {
          left: radius + Math.cos(port.angle) * radius,
          top: radius + Math.sin(port.angle) * radius,
          right: "auto" as const,
          bottom: "auto" as const,
          transform: "translate(-50%, -50%)",
          opacity: 0,
          width: 2,
          height: 2,
          minWidth: 0,
          minHeight: 0,
          pointerEvents: "none" as const
        };
        return [
          <Handle
            id={`target-${port.name}`}
            isConnectable={false}
            key={`target-${port.name}`}
            position={port.position}
            style={style}
            type="target"
          />,
          <Handle
            id={`source-${port.name}`}
            isConnectable={false}
            key={`source-${port.name}`}
            position={port.position}
            style={style}
            type="source"
          />
        ];
      })}
      <button
        aria-label={`查看${KIND_LABELS[data.kind]}：${data.title}`}
        aria-pressed={data.active}
        className={[
          "wiki-graph-card",
          `wiki-graph-${data.kind}`,
          data.muted ? "wiki-graph-muted" : "",
          data.highlighted ? "wiki-graph-highlighted" : ""
        ].filter(Boolean).join(" ")}
        onBlur={data.onBlur}
        onClick={(event) => {
          event.stopPropagation();
          data.onFocus(data.path);
          data.onOpen(data.path);
        }}
        onFocus={() => data.onFocus(data.path)}
        style={{
          width: data.diameter + data.graphWidth + 12,
          height: data.diameter,
          "--wiki-node-diameter": `${data.diameter}px`
        } as CSSProperties}
        title={data.title}
        type="button"
      >
        <span
          aria-hidden="true"
          className="wiki-graph-node-dot"
        />
        <span className="wiki-graph-node-label"
          style={{ left: data.diameter + 12, width: data.graphWidth }}>
          <small>{KIND_LABELS[data.kind]}{data.degree ? ` · ${data.degree} 条关联` : ""}</small>
          <strong>{data.label}</strong>
        </span>
      </button>
    </>
  );
}

function ZoomInIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <circle cx="10.8" cy="10.8" r="6.8" />
      <path d="M16 16l4.5 4.5M10.8 7.8v6M7.8 10.8h6" />
    </svg>
  );
}

function ZoomOutIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <circle cx="10.8" cy="10.8" r="6.8" />
      <path d="M16 16l4.5 4.5M7.8 10.8h6" />
    </svg>
  );
}

function FullscreenIcon({ active }: { active: boolean }) {
  return active ? (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <path d="M8 3v5H3M16 3v5h5M8 21v-5H3m13 5v-5h5" />
      <path d="M8 8 3 3m13 5 5-5M8 16l-5 5m13-5 5 5" />
    </svg>
  ) : (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <path d="M8 3H3v5m13-5h5v5M8 21H3v-5m13 5h5v-5" />
    </svg>
  );
}

const nodeTypes = { wiki: WikiNode };

export function WikiGraphCanvas({
  graph,
  selectedPath,
  onOpen
}: {
  graph: WikiGraph;
  selectedPath: string | null;
  onOpen: (path: string) => void;
}) {
  const graphElement = useRef<HTMLDivElement>(null);
  const graphSize = useRef<{ width: number; height: number } | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [fullscreenError, setFullscreenError] = useState<string | null>(null);
  // A published revision always has the same pages and relationships.
  const layout = useMemo(
    () => layoutWikiGraph(graph),
    [graph.revision]
  );
  const [focusedPath, setFocusedPath] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [visibleKinds, setVisibleKinds] = useState<Set<WikiGraphNode["kind"]>>(
    () => new Set(GRAPH_KINDS)
  );
  const [flow, setFlow] = useState<ReactFlowInstance<WikiFlowNode, Edge> | null>(null);
  useEffect(() => {
    setFocusedPath(null);
    setVisibleKinds(new Set(GRAPH_KINDS));
    void flow?.fitView({ padding: 0.2, duration: 350, maxZoom: 2.25 });
  }, [graph.revision, flow]);
  useEffect(() => {
    const element = graphElement.current;
    if (!element || !flow) return;
    let frame = 0;
    let timer = 0;
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      const { width, height } = entry.contentRect;
      const previous = graphSize.current;
      graphSize.current = { width, height };
      if (!width || !height || !previous || (previous.width === width && previous.height === height)) return;
      window.clearTimeout(timer);
      cancelAnimationFrame(frame);
      timer = window.setTimeout(() => {
        frame = requestAnimationFrame(() => {
          void flow.fitView({ padding: 0.2, duration: 200, maxZoom: 2.25 });
        });
      }, 120);
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
      window.clearTimeout(timer);
      cancelAnimationFrame(frame);
    };
  }, [flow]);
  useEffect(() => {
    const updateFullscreenState = () => {
      setIsFullscreen(document.fullscreenElement === graphElement.current);
    };
    document.addEventListener("fullscreenchange", updateFullscreenState);
    return () => document.removeEventListener("fullscreenchange", updateFullscreenState);
  }, []);
  const toggleFullscreen = useCallback(async () => {
    const element = graphElement.current;
    if (!element) return;
    try {
      if (document.fullscreenElement === element) await document.exitFullscreen();
      else await element.requestFullscreen();
      setFullscreenError(null);
    } catch (cause) {
      setFullscreenError(cause instanceof Error ? cause.message : "无法切换全屏模式。");
    }
  }, []);
  const focused = useMemo(
    () => graphFocusPaths(graph, focusedPath), [graph, focusedPath]
  );
  const graphDegrees = useMemo(() => {
    const values = new Map(layout.nodes.map((node) => [node.path, 0]));
    layout.edges.forEach((edge) => {
      values.set(edge.source, (values.get(edge.source) ?? 0) + 1);
      values.set(edge.target, (values.get(edge.target) ?? 0) + 1);
    });
    return values;
  }, [layout]);
  const visibleNodes = useMemo(
    () => layout.nodes.filter((node) => visibleKinds.has(node.kind)),
    [layout.nodes, visibleKinds]
  );
  const visiblePaths = useMemo(
    () => new Set(visibleNodes.map((node) => node.path)),
    [visibleNodes]
  );
  const visibleEdges = useMemo(
    () => layout.edges.filter((edge) =>
      visiblePaths.has(edge.source) && visiblePaths.has(edge.target)
    ),
    [layout.edges, visiblePaths]
  );
  const degrees = useMemo(() => {
    const values = new Map(visibleNodes.map((node) => [node.path, 0]));
    visibleEdges.forEach((edge) => {
      values.set(edge.source, (values.get(edge.source) ?? 0) + 1);
      values.set(edge.target, (values.get(edge.target) ?? 0) + 1);
    });
    return values;
  }, [visibleNodes, visibleEdges]);
  const nodes: WikiFlowNode[] = useMemo(() => visibleNodes.map((node) => {
    const size = graphNodeSize(node.graphWidth, graphDegrees.get(node.path) ?? 0);
    const diameter = size.initialHeight;
    return {
      ...size,
      id: node.path,
      type: "wiki" as const,
      position: {
        x: node.position.x - diameter / 2,
        y: node.position.y - diameter / 2
      },
      data: {
        path: node.path,
        title: node.title,
        label: node.title,
        kind: node.kind,
        active: selectedPath === node.path,
        degree: degrees.get(node.path) ?? 0,
        diameter,
        graphWidth: node.graphWidth,
        muted: focusedPath !== null && !focused.has(node.path),
        highlighted: focusedPath === node.path,
        onOpen,
        onFocus: setFocusedPath,
        onBlur: () => setFocusedPath((current) => current === node.path ? null : current)
      },
      draggable: false,
      selectable: true,
      focusable: true
    };
  }), [visibleNodes, selectedPath, onOpen, degrees, graphDegrees, focusedPath, focused]);
  const edges: Edge[] = useMemo(() => {
    const positions = new Map(layout.nodes.map((node) => [node.path, node.position]));
    return visibleEdges.map((edge, index) => {
      const from = positions.get(edge.source)!;
      const to = positions.get(edge.target)!;
      const angle = Math.atan2(to.y - from.y, to.x - from.x);
      const sourcePort = getGraphPort(angle);
      const targetPort = getGraphPort(angle + Math.PI);
      return {
        id: `${edge.source}:${edge.target}:${index}`,
        source: edge.source,
        target: edge.target,
        sourceHandle: `source-${sourcePort}`,
        targetHandle: `target-${targetPort}`,
        type: "straight",
        className: [
          "wiki-graph-edge",
          `wiki-graph-edge-${edge.kind}`,
          focusedPath && edge.source !== focusedPath && edge.target !== focusedPath
            ? "wiki-graph-edge-muted" : "",
          focusedPath && (edge.source === focusedPath || edge.target === focusedPath)
            ? "wiki-graph-edge-highlighted" : ""
        ].filter(Boolean).join(" "),
        label: focusedPath && (edge.source === focusedPath || edge.target === focusedPath)
          ? edge.kind === "source" ? "来源" : "关联" : undefined,
        labelStyle: { fill: "var(--text-secondary)", fontSize: 10 },
        labelBgStyle: { fill: "var(--surface)", fillOpacity: 0.95 },
        selectable: false
      };
    });
  }, [layout.nodes, visibleEdges, focusedPath]);
  const matches = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("zh-CN");
    return query ? graph.nodes.filter((node) =>
      `${node.title} ${node.path}`.toLocaleLowerCase("zh-CN").includes(query)
    ).slice(0, 7) : [];
  }, [graph.nodes, search]);

  function locate(path: string, open = false) {
    const node = layout.nodes.find((item) => item.path === path);
    const graphNode = graph.nodes.find((item) => item.path === path);
    if (graphNode && !visibleKinds.has(graphNode.kind)) {
      setVisibleKinds((current) => new Set([...current, graphNode.kind]));
    }
    if (node && flow) {
      void flow.setCenter(node.position.x, node.position.y, {
        zoom: 1.15,
        duration: 450
      });
      setFocusedPath(path);
    }
    setSearch("");
    if (open) onOpen(path);
  }

  function toggleKind(kind: WikiGraphNode["kind"]) {
    setVisibleKinds((current) => {
      const next = new Set(current);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
    setFocusedPath(null);
  }

  return (
    <div className="wiki-graph" aria-label="知识页面关系图" ref={graphElement}>
      {fullscreenError ? (
        <div className="wiki-graph-error" role="status">{fullscreenError}</div>
      ) : null}
      <div className="wiki-graph-search">
        <input
          aria-label="查找知识节点"
          onChange={(event) => setSearch(event.target.value)}
          placeholder="查找节点标题…"
          type="search"
          value={search}
        />
        {search.trim() ? (
          <div className="wiki-graph-search-results" role="region" aria-label="匹配节点">
            {matches.length ? matches.map((node) => (
              <button disabled={!flow} key={node.path} onClick={() => locate(node.path, true)} type="button">
                <strong>{node.title}</strong>
                <small>{KIND_LABELS[node.kind]}</small>
              </button>
            )) : <span>没有匹配的节点</span>}
          </div>
        ) : null}
      </div>
      <div className="wiki-graph-legend" aria-label="节点类型">
        <strong>节点类型</strong>
        {GRAPH_KINDS.map((kind) => {
          const count = graph.nodes.filter((node) => node.kind === kind).length;
          return count ? (
            <button
              aria-pressed={visibleKinds.has(kind)}
              className={`wiki-graph-filter wiki-graph-legend-${kind}${visibleKinds.has(kind) ? " wiki-graph-filter-active" : ""}`}
              key={kind}
              onClick={() => toggleKind(kind)}
              type="button"
            >
              <span className={`wiki-graph-filter-dot wiki-graph-filter-dot-${kind}`} />
              {KIND_LABELS[kind]} {count}
            </button>
          ) : null;
        })}
      </div>
      <ReactFlow
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 2.25 }}
        maxZoom={2.5}
        minZoom={0.25}
        nodes={nodes}
        nodeTypes={nodeTypes}
        nodesConnectable={false}
        nodesDraggable={false}
        nodesFocusable
        onNodeClick={(_, node) => node.data.onOpen(node.data.path)}
        edgesFocusable
        onEdgeClick={(_, edge) => setFocusedPath(edge.source)}
        onInit={setFlow}
        onPaneClick={() => setFocusedPath(null)}
      >
        <Background color="var(--graph-grid)" gap={22} />
        <Controls position="top-right" showFitView={false} showInteractive={false} showZoom={false}>
          <ControlButton aria-label="放大图谱" onClick={() => void flow?.zoomIn()} title="放大">
            <ZoomInIcon />
          </ControlButton>
          <ControlButton aria-label="缩小图谱" onClick={() => void flow?.zoomOut()} title="缩小">
            <ZoomOutIcon />
          </ControlButton>
          <ControlButton
            aria-label={isFullscreen ? "退出全屏" : "全屏显示"}
            onClick={() => void toggleFullscreen()}
            title={isFullscreen ? "退出全屏" : "全屏显示"}
          >
            <FullscreenIcon active={isFullscreen} />
          </ControlButton>
        </Controls>
        {visibleNodes.length > 5 ? (
          <MiniMap<WikiFlowNode>
            ariaLabel="图谱缩略导航"
            bgColor="var(--surface-subtle)"
            className="wiki-graph-minimap"
            maskColor="var(--graph-mask)"
            maskStrokeColor="var(--accent)"
            maskStrokeWidth={1.3}
            nodeComponent={WikiMiniMapNode}
            nodeStrokeColor={(node) => node.data.active ? "var(--accent-strong)" : "var(--surface)"}
            nodeStrokeWidth={0.8}
            pannable
            nodeColor={(node) => {
              const kind = node.data.kind;
              const color = `var(--graph-${kind})`;
              return node.data.muted ? `color-mix(in srgb, ${color} 33%, transparent)` : color;
            }}
            style={{ width: 176, height: 112 }}
            zoomable
          />
        ) : null}
      </ReactFlow>
    </div>
  );
}
