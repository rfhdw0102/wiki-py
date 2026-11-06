export function simulationBadge(tags: readonly string[]) {
  return tags.includes("dingtalk-mock") || tags.includes("simulation")
    ? "模拟钉钉 · 未连接真实钉钉"
    : null;
}
