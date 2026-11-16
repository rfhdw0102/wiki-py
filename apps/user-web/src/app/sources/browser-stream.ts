import type { BrowserAction } from "@wiki-agent/web-ui";

export type BrowserLiveFrame = {
  image: string;
  url: string;
  title: string;
  width: number;
  height: number;
  can_go_back?: boolean;
  resource_errors?: number;
};

const MAX_EVENT = 2 * 1024 * 1024;

function frameEvent(event: string): BrowserLiveFrame | null {
  const lines = event.split("\n");
  const kind = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
  const data = lines.filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart()).join("\n");
  if (!data || (kind !== "frame" && kind !== "error")) return null;
  let value: unknown;
  try { value = JSON.parse(data); }
  catch { throw new Error("实时网页数据无效，请重新连接。"); }
  if (kind === "error") {
    const detail = value && typeof value === "object" && "detail" in value
      ? value.detail : value;
    const code = detail && typeof detail === "object" && "code" in detail &&
      typeof detail.code === "string" ? detail.code.toLowerCase() : "";
    throw new Error(code === "session_expired" || code === "session_not_found"
      ? "网页会话已结束，请关闭后重新开始导入。"
      : "实时网页连接已断开，请重新连接。");
  }
  if (!value || typeof value !== "object" ||
    !("image" in value) || typeof value.image !== "string" ||
    !value.image.startsWith("/9j/") || !/^[A-Za-z0-9+/]+={0,2}$/.test(value.image) ||
    !("url" in value) || typeof value.url !== "string" || value.url.length > 8192 ||
    !("title" in value) || typeof value.title !== "string" || value.title.length > 512 ||
    !("width" in value) || value.width !== 1024 ||
    !("height" in value) || value.height !== 768) {
    throw new Error("实时网页画面无效，请重新连接。");
  }
  return {
    image: value.image, url: value.url, title: value.title, width: 1024, height: 768,
    can_go_back: "can_go_back" in value && typeof value.can_go_back === "boolean"
      ? value.can_go_back : undefined,
    resource_errors: "resource_errors" in value && typeof value.resource_errors === "number" &&
      Number.isInteger(value.resource_errors) && value.resource_errors > 0
      ? value.resource_errors : 0
  };
}

export async function* browserFrames(
  stream: ReadableStream<Uint8Array>
): AsyncGenerator<BrowserLiveFrame> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) throw new Error("实时网页连接已断开，请重新连接。");
      buffer = (buffer + decoder.decode(chunk.value, { stream: true })).replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const event = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (event.length > MAX_EVENT) throw new Error("实时网页画面过大，请重新连接。");
        const frame = frameEvent(event);
        if (frame) yield frame;
      }
      if (buffer.length > MAX_EVENT) throw new Error("实时网页画面过大，请重新连接。");
    }
  } finally {
    try { await reader.cancel(); }
    finally { reader.releaseLock(); }
  }
}

export function fitBrowserViewport(width: number, height: number) {
  const scale = Math.max(0, Math.min(width / 1024, height / 768));
  return { width: Math.floor(1024 * scale), height: Math.floor(768 * scale) };
}

export function browserKeyAction(event: {
  key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean;
}): BrowserAction | null {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    return { action: "key", key: "Control+A" };
  }
  if (event.altKey && event.key === "ArrowLeft") return { action: "back" };
  const keys: Record<string, BrowserAction> = {
    Enter: { action: "key", key: "Enter" }, Tab: { action: "key", key: "Tab" },
    Backspace: { action: "key", key: "Backspace" }, Escape: { action: "key", key: "Escape" },
    ArrowLeft: { action: "key", key: "ArrowLeft" }, ArrowRight: { action: "key", key: "ArrowRight" },
    ArrowUp: { action: "key", key: "ArrowUp" }, ArrowDown: { action: "key", key: "ArrowDown" }
  };
  return keys[event.key] ?? null;
}

export function browserTextActions(text: string): BrowserAction[] {
  if (text.length > 10000) throw new Error("一次输入不能超过 10000 个字符，请分段输入。");
  const actions: BrowserAction[] = [];
  while (text) {
    let end = Math.min(1000, text.length);
    if (end < text.length && /[\uD800-\uDBFF]/.test(text[end - 1])) end--;
    actions.push({ action: "type", text: text.slice(0, end) });
    text = text.slice(end);
  }
  return actions;
}

export class BrowserInputQueue {
  private queue: BrowserAction[] = [];
  private running: Promise<void> | null = null;
  private failure: unknown = null;
  private disposed = false;
  private send: (action: BrowserAction) => Promise<unknown>;
  private onError: (cause: unknown) => void;

  constructor(
    send: (action: BrowserAction) => Promise<unknown>,
    onError: (cause: unknown) => void
  ) {
    this.send = send;
    this.onError = onError;
  }

  enqueue(action: BrowserAction) {
    if (this.disposed || this.failure) return;
    const last = this.queue.at(-1);
    if (action.action === "pointer" && action.phase === "move" &&
      last?.action === "pointer" && last.phase === "move") {
      this.queue[this.queue.length - 1] = action;
    } else if (action.action === "type" && last?.action === "type" &&
      last.text.length + action.text.length <= 1000) {
      this.queue[this.queue.length - 1] = { action: "type", text: last.text + action.text };
    } else if (action.action === "scroll" && last?.action === "scroll" &&
      Math.abs(last.delta + action.delta) <= 1200) {
      this.queue[this.queue.length - 1] = { action: "scroll", delta: last.delta + action.delta };
    } else {
      if (this.queue.length >= 128) {
        this.fail(new Error("网页响应较慢，操作已暂停，请重新连接后再试。"));
        return;
      }
      this.queue.push(action);
    }
    if (!this.running) this.running = Promise.resolve().then(() => this.run());
  }

  private fail(cause: unknown) {
    this.failure = cause;
    this.queue = [];
    if (!this.disposed) this.onError(cause);
  }

  private async run() {
    try {
      while (!this.disposed && this.queue.length) {
        const action = this.queue.shift();
        if (action) await this.send(action);
      }
    } catch (cause) {
      this.fail(cause);
    } finally {
      this.running = null;
    }
  }

  async flush() {
    while (this.running) await this.running;
    if (this.failure) throw this.failure;
  }

  dispose() {
    this.disposed = true;
    this.queue = [];
  }
}
