import type { BrowserAction } from "@wiki-agent/web-ui";

export type BrowserPoint = { x: number; y: number };

export function browserPoint(
  clientX: number, clientY: number,
  bounds: { left: number; top: number; width: number; height: number },
  width: number, height: number
): BrowserPoint {
  if (bounds.width <= 0 || bounds.height <= 0 || width <= 0 || height <= 0) {
    throw new Error("浏览器画面尺寸无效，请刷新画面后重试。");
  }
  return {
    x: Math.min(width - 1, Math.max(0, Math.floor((clientX - bounds.left) / bounds.width * width))),
    y: Math.min(height - 1, Math.max(0, Math.floor((clientY - bounds.top) / bounds.height * height)))
  };
}

export function browserPointerAction(start: BrowserPoint, end: BrowserPoint): BrowserAction {
  return Math.hypot(end.x - start.x, end.y - start.y) > 5
    ? { action: "drag", x: start.x, y: start.y, to_x: end.x, to_y: end.y }
    : { action: "click", x: start.x, y: start.y };
}
