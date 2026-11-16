import assert from "node:assert/strict";
import test from "node:test";
import { getNodesBounds } from "@xyflow/react";
import {
  graphFocusPaths, graphNeighbors, graphNodeSize, layoutWikiGraph
} from "../src/app/wiki/graph-layout.ts";
import {
  renderWikiCitationMarkers, wikiCitationEntries
} from "../src/app/wiki/citations.ts";

const graph = {
  revision: "a",
  nodes: [
    { path: "entities/cordis.md", kind: "entity", title: "Cordis" },
    { path: "sources/first.md", kind: "source", title: "Document title" },
    { path: "concepts/overview.md", kind: "concept", title: "Overview" },
    { path: "sources/second.md", kind: "source", title: "Another document" }
  ],
  edges: [
    { source: "entities/cordis.md", target: "sources/first.md", kind: "source" },
    { source: "entities/cordis.md", target: "concepts/overview.md", kind: "related" },
    { source: "entities/cordis.md", target: "elsewhere/missing.md", kind: "related" }
  ]
};

test("fixed node dimensions are available to the minimap before DOM measurements", () => {
  for (const degree of [0, 1, 4, 100]) {
    const size = graphNodeSize(150, degree);
    const diameter = 25 + Math.min(Math.sqrt(degree) * 4, 12);
    assert.equal(size.initialHeight, diameter);
    assert.equal(size.initialWidth, 150 + diameter + 12);
    assert.deepEqual(size.style, {
      width: size.initialWidth, height: size.initialHeight
    });
    assert.deepEqual(getNodesBounds([{
      id: "test", position: { x: 10, y: 20 }, data: {}, ...size
    }]), {
      x: 10, y: 20, width: size.initialWidth, height: size.initialHeight
    });
  }
});

test("all titled nodes including isolated pages get deterministic, distinct positions", () => {
  const first = layoutWikiGraph(graph);
  assert.deepEqual(layoutWikiGraph(graph), first);
  assert.equal(first.nodes.length, graph.nodes.length);
  assert.equal(first.edges.length, 2);
  const source = first.nodes.find((node) => node.path === "sources/first.md");
  const entity = first.nodes.find((node) => node.path === "entities/cordis.md");
  assert.equal(source.title, "Document title");
  assert.notDeepEqual(source.position, entity.position);
  assert.notDeepEqual(source.position, first.nodes.find(
    (node) => node.path === "sources/second.md"
  ).position);
  assert.ok(first.nodes.every((node) =>
    Number.isFinite(node.position.x) && Number.isFinite(node.position.y)
  ));
});

test("node bounds reserve space for always-visible graph labels", () => {
  const layout = layoutWikiGraph(graph);
  const source = layout.nodes.find((node) => node.path === "sources/first.md");
  const entity = layout.nodes.find((node) => node.path === "entities/cordis.md");
  assert.ok(source.graphWidth > entity.graphWidth);
  assert.ok(layout.nodes.every((node) =>
    node.graphWidth >= 110 && node.graphWidth <= 300
  ));
});

test("node hit areas do not overlap after layout", () => {
  const layout = layoutWikiGraph(graph);
  const degrees = new Map(layout.nodes.map((node) => [node.path, 0]));
  for (const edge of layout.edges) {
    degrees.set(edge.source, degrees.get(edge.source) + 1);
    degrees.set(edge.target, degrees.get(edge.target) + 1);
  }
  const bounds = layout.nodes.map((node) => {
    const diameter = 25 + Math.min(Math.sqrt(degrees.get(node.path)) * 4, 12);
    return {
      left: node.position.x - diameter / 2,
      top: node.position.y - diameter / 2,
      right: node.position.x + node.graphWidth + 12 + diameter / 2,
      bottom: node.position.y + diameter / 2
    };
  });
  for (let first = 0; first < bounds.length; first++) {
    for (let second = first + 1; second < bounds.length; second++) {
      const a = bounds[first];
      const b = bounds[second];
      assert.ok(
        a.right <= b.left || b.right <= a.left ||
        a.bottom <= b.top || b.bottom <= a.top,
        "node hit areas should not overlap"
      );
    }
  }
});

test("connected nodes tend to form a group rather than being sorted into type columns", () => {
  const layout = layoutWikiGraph(graph);
  const position = (path) => layout.nodes.find((node) => node.path === path).position;
  const distance = (a, b) =>
    Math.hypot(position(a).x - position(b).x, position(a).y - position(b).y);
  assert.ok(
    distance("entities/cordis.md", "sources/first.md") <
    distance("entities/cordis.md", "sources/second.md")
  );
});

test("layout spacing scales the graph without changing its relationships", () => {
  const compact = layoutWikiGraph(graph);
  const expanded = layoutWikiGraph(graph, 1.5);
  for (const node of compact.nodes) {
    const expandedNode = expanded.nodes.find((item) => item.path === node.path);
    assert.ok(expandedNode);
    assert.ok(Math.abs(expandedNode.position.x - node.position.x * 1.5) <= 1);
    assert.ok(Math.abs(expandedNode.position.y - node.position.y * 1.5) <= 1);
  }
  assert.deepEqual(expanded.edges, compact.edges);
});

test("connected pages are discoverable from either end of a relationship", () => {
  assert.deepEqual(
    graphNeighbors(graph, "entities/cordis.md").map((node) => node.title),
    ["Document title", "Overview"]
  );
  assert.deepEqual(
    graphNeighbors(graph, "sources/first.md").map((node) => node.title),
    ["Cordis"]
  );
});

test("focus keeps only the hovered node and its direct neighbors prominent", () => {
  assert.deepEqual(
    [...graphFocusPaths(graph, "entities/cordis.md")].sort(),
    ["concepts/overview.md", "entities/cordis.md", "sources/first.md"]
  );
  assert.equal(graphFocusPaths(graph, null).size, 0);
});

test("rich text citation markers become numbered links to their evidence entries", () => {
  const entries = wikiCitationEntries({
    ahead: {
      source_revision_id: "revision-1",
      span_id: "revision-1:4",
      label: "Codex 领先项"
    },
    three: {
      source_revision_id: "revision-1",
      span_id: "revision-1:8",
      label: "三项特性"
    }
  });
  assert.deepEqual(entries.map(({ key, number }) => [key, number]), [
    ["ahead", 1],
    ["three", 2]
  ]);
  assert.equal(
    renderWikiCitationMarkers("领先项[^ahead]；三项特性[^three]。", entries),
    "领先项[1](#wiki-citation-ahead)；三项特性[2](#wiki-citation-three)。"
  );
});

test("unknown citation markers remain unchanged instead of linking to missing evidence", () => {
  assert.equal(
    renderWikiCitationMarkers("未映射引用[^missing]", []),
    "未映射引用[^missing]"
  );
});
