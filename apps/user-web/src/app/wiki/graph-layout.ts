import type { WikiGraph, WikiGraphNode } from "@wiki-agent/web-ui";
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum
} from "d3-force";

export const GRAPH_KINDS = ["source", "entity", "concept", "pending"] as const;

type LayoutNode = SimulationNodeDatum & {
  id: string;
  radius: number;
  anchorOffset: number;
  width: number;
};
type LayoutLink = SimulationLinkDatum<LayoutNode> & {
  kind: WikiGraph["edges"][number]["kind"];
};

export function graphNodeSize(labelWidth: number, degree: number) {
  const height = 25 + Math.min(Math.sqrt(degree) * 4, 12);
  const width = labelWidth + height + 12;
  return {
    initialWidth: width,
    initialHeight: height,
    style: { width, height }
  };
}

export function layoutWikiGraph(graph: WikiGraph, spacing = 1) {
  const validPaths = new Set(graph.nodes.map((node) => node.path));
  const edges = graph.edges.filter(
    (edge) => validPaths.has(edge.source) && validPaths.has(edge.target)
  );
  const sorted = [...graph.nodes].sort((a, b) => a.path.localeCompare(b.path));
  const degrees = new Map(sorted.map((node) => [node.path, 0]));
  for (const edge of edges) {
    degrees.set(edge.source, (degrees.get(edge.source) ?? 0) + 1);
    degrees.set(edge.target, (degrees.get(edge.target) ?? 0) + 1);
  }
  const nodes: LayoutNode[] = sorted.map((node, index) => {
    const angle = index * 2.399963229728653;
    const radius = Math.sqrt(index + 1) * 64;
    const titleWidth = [...node.title].reduce(
      (width, char) => width + ((char.codePointAt(0) ?? 0) > 0x2e80 ? 13 : 7),
      0
    );
    const width = 34 + Math.min(266, Math.max(76, titleWidth));
    const size = graphNodeSize(width, degrees.get(node.path) ?? 0);
    const diameter = size.initialHeight;
    const anchorOffset = (width + 12) / 2;
    const halfWidth = size.initialWidth / 2;
    return {
      id: node.path,
      x: Math.cos(angle) * radius + anchorOffset,
      y: Math.sin(angle) * radius,
      width,
      anchorOffset,
      radius: Math.hypot(halfWidth, diameter / 2) + 16
    };
  });
  const links: LayoutLink[] = edges.map((edge) => ({
    source: edge.source,
    target: edge.target,
    kind: edge.kind
  }));

  if (nodes.length > 1) {
    const simulation = forceSimulation(nodes)
      .force("link", forceLink<LayoutNode, LayoutLink>(links)
        .id((node) => node.id)
        .distance((link) => link.kind === "source" ? 270 : 300)
        .strength(0.38))
      .force("charge", forceManyBody().strength(-740).distanceMax(1100))
      .force("collision", forceCollide<LayoutNode>().radius((node) => node.radius + 28))
      .force("center", forceCenter(0, 0))
      .stop();
    for (let tick = 0; tick < Math.min(280, 150 + nodes.length * 2); tick++) {
      simulation.tick();
    }
  }
  const positions = new Map(nodes.map((node) => [
    node.id,
    {
      x: Math.round(((node.x ?? 0) - node.anchorOffset) * spacing),
      y: Math.round((node.y ?? 0) * spacing)
    }
  ]));
  const widths = new Map(nodes.map((node) => [node.id, node.width]));
  return {
    nodes: graph.nodes.map((node) => ({
      ...node,
      graphWidth: widths.get(node.path) ?? 150,
      position: positions.get(node.path)!
    })),
    edges
  };
}

export function graphNeighbors(graph: WikiGraph, path: string): WikiGraphNode[] {
  const paths = new Set(graph.edges.flatMap((edge) =>
    edge.source === path ? [edge.target] : edge.target === path ? [edge.source] : []
  ));
  return graph.nodes.filter((node) => paths.has(node.path));
}

export function graphFocusPaths(graph: WikiGraph, path: string | null): Set<string> {
  return path ? new Set([path, ...graphNeighbors(graph, path).map((node) => node.path)])
    : new Set();
}
