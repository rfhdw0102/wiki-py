"use client";

import { ApiError, api, type BrowserAction, type BrowserSession, type Source } from "@wiki-agent/web-ui";
import { useCallback, useEffect, useRef, useState } from "react";
import { browserPoint, type BrowserPoint } from "./browser-controls";
import {
  BrowserInputQueue, browserFrames, browserKeyAction, browserTextActions, fitBrowserViewport,
  type BrowserLiveFrame
} from "./browser-stream";

export function WebBrowserImport({
  url, onImported, onClosed, onCleanupError
}: {
  url: string; onImported: (source: Source) => void; onClosed: () => void;
  onCleanupError: (message: string) => void;
}) {
  const [session, setSession] = useState<BrowserSession | null>(null);
  const [connected, setConnected] = useState(false);
  const [opening, setOpening] = useState(true);
  const [saving, setSaving] = useState(false);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resourceErrors, setResourceErrors] = useState(0);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const frame = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const keyboard = useRef<HTMLTextAreaElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const sessionId = useRef<string | null>(null);
  const connection = useRef<AbortController | null>(null);
  const inputs = useRef<BrowserInputQueue | null>(null);
  const mounted = useRef(true);
  const ready = useRef(false);
  const locked = useRef(false);
  const composing = useRef(false);
  const dragging = useRef(false);
  const lastPoint = useRef<BrowserPoint | null>(null);
  const moveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollDelta = useRef(0);
  const decodedImage = useRef<HTMLImageElement | null>(null);

  ready.current = connected && !saving && !paused;

  const send = useCallback((action: BrowserAction) => {
    if (ready.current) inputs.current?.enqueue(action);
  }, []);

  const releasePointer = useCallback(() => {
    if (dragging.current && lastPoint.current) {
      send({ action: "pointer", phase: "up", ...lastPoint.current });
    }
    dragging.current = false;
    if (moveTimer.current) clearTimeout(moveTimer.current);
    moveTimer.current = null;
  }, [send]);

  const connect = useCallback((id: string) => {
    ready.current = false;
    connection.current?.abort();
    inputs.current?.dispose();
    if (decodedImage.current) {
      decodedImage.current.onload = null;
      decodedImage.current.onerror = null;
      decodedImage.current.src = "";
    }
    const controller = new AbortController();
    connection.current = controller;
    setConnected(false);
    setPaused(false);
    setError(null);
    inputs.current = new BrowserInputQueue(
      (action) => api.browserSessionAction(id, action, controller.signal),
      (cause) => {
        if (!controller.signal.aborted && mounted.current) {
          ready.current = false;
          setPaused(true);
          setError(cause instanceof Error ? cause.message : "网页操作失败，请重新连接。");
        }
      }
    );
    let decoding = false;
    let latest: BrowserLiveFrame | null = null;
    function paint(update: BrowserLiveFrame) {
      if (controller.signal.aborted || !mounted.current) return;
      latest = update;
      if (!decoding) decodeLatest();
    }
    function decodeLatest() {
      const update = latest;
      latest = null;
      if (!update || controller.signal.aborted || !mounted.current) return;
      decoding = true;
      const image = new Image();
      decodedImage.current = image;
      image.onload = () => {
        if (controller.signal.aborted || !mounted.current) return;
        canvas.current?.getContext("2d", { alpha: false })?.drawImage(image, 0, 0, 1024, 768);
        setConnected(true);
        setResourceErrors(update.resource_errors ?? 0);
        setSession((current) => current && (current.url !== update.url || current.title !== update.title)
          ? { ...current, url: update.url, title: update.title } : current);
        image.onload = null;
        image.onerror = null;
        image.src = "";
        decoding = false;
        if (latest) decodeLatest();
      };
      image.onerror = () => {
        if (!controller.signal.aborted && mounted.current) {
          ready.current = false;
          setConnected(false);
          setError("实时网页画面无法显示，请重新连接。");
          controller.abort();
          inputs.current?.dispose();
        }
      };
      image.src = `data:image/jpeg;base64,${update.image}`;
    }
    void (async () => {
      try {
        const stream = await api.browserSessionFrames(id, controller.signal);
        for await (const update of browserFrames(stream)) paint(update);
      } catch (cause) {
        if (!controller.signal.aborted && mounted.current) {
          ready.current = false;
          controller.abort();
          inputs.current?.dispose();
          setConnected(false);
          setError(cause instanceof Error ? cause.message : "实时网页连接中断，请重新连接。");
        }
      }
    })();
  }, []);

  useEffect(() => {
    let cancelled = false;
    mounted.current = true;
    void (async () => {
      try {
        const opened = await api.openBrowserSession(url);
        if (cancelled) {
          await api.closeBrowserSession(opened.id);
          return;
        }
        sessionId.current = opened.id;
        setSession(opened);
        connect(opened.id);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "无法打开网页，请稍后重试。");
        else onCleanupError("无法确认网页会话已清理，如已创建将在到期后自动清除。");
      } finally {
        if (!cancelled) setOpening(false);
      }
    })();
    return () => {
      cancelled = true;
      mounted.current = false;
      connection.current?.abort();
      inputs.current?.dispose();
      if (moveTimer.current) clearTimeout(moveTimer.current);
      if (scrollTimer.current) clearTimeout(scrollTimer.current);
      if (decodedImage.current) {
        decodedImage.current.onload = null;
        decodedImage.current.onerror = null;
        decodedImage.current.src = "";
      }
      const id = sessionId.current;
      sessionId.current = null;
      if (id) void api.closeBrowserSession(id).catch((cause: unknown) => {
        if (!(cause instanceof ApiError && cause.status === 404)) {
          onCleanupError("网页会话清理失败，将在到期后自动清除。");
        }
      });
    };
  }, [url, connect, onCleanupError]);

  useEffect(() => {
    const element = frame.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      setSize(fitBrowserViewport(element.clientWidth, element.clientHeight));
    });
    observer.observe(element);
    function scroll(event: WheelEvent) {
      if (!ready.current) return;
      event.preventDefault();
      scrollDelta.current += event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 768 : 1);
      if (scrollTimer.current) return;
      function flush() {
        const delta = Math.max(-1200, Math.min(1200, Math.round(scrollDelta.current)));
        scrollDelta.current -= delta;
        if (delta) send({ action: "scroll", delta });
        scrollTimer.current = Math.abs(scrollDelta.current) >= 1 ? setTimeout(flush, 32) : null;
      }
      scrollTimer.current = setTimeout(flush, 32);
    }
    element.addEventListener("wheel", scroll, { passive: false });
    window.addEventListener("blur", releasePointer);
    return () => {
      observer.disconnect();
      element.removeEventListener("wheel", scroll);
      window.removeEventListener("blur", releasePointer);
    };
  }, [send, releasePointer]);

  function point(event: { clientX: number; clientY: number }) {
    const bounds = canvas.current?.getBoundingClientRect();
    return bounds ? browserPoint(event.clientX, event.clientY, bounds, 1024, 768) : null;
  }

  function enterText(text: string) {
    if (!ready.current || !text) return;
    try { for (const action of browserTextActions(text)) send(action); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "无法输入文字。"); }
  }

  async function close() {
    if (locked.current) return;
    locked.current = true;
    setSaving(true);
    ready.current = false;
    connection.current?.abort();
    inputs.current?.dispose();
    try {
      const id = sessionId.current;
      if (id) await api.closeBrowserSession(id);
      sessionId.current = null;
      onClosed();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        sessionId.current = null;
        onClosed();
      } else {
        setError(cause instanceof Error ? cause.message : "关闭网页失败，请重试。");
        setConnected(false);
      }
    } finally {
      locked.current = false;
      if (mounted.current) setSaving(false);
    }
  }

  async function confirmImport() {
    const id = sessionId.current;
    if (!id || locked.current || !ready.current) return;
    locked.current = true;
    ready.current = false;
    setSaving(true);
    setError(null);
    try {
      await inputs.current?.flush();
      const source = await api.importBrowserSession(id);
      sessionId.current = null;
      connection.current?.abort();
      inputs.current?.dispose();
      onImported(source);
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : "当前页面没有有效正文，请完成验证后再确认。");
    } finally {
      locked.current = false;
      if (mounted.current) setSaving(false);
    }
  }

  return (
    <div className="settings-modal-backdrop web-browser-backdrop" role="presentation">
      <section className="settings-modal web-browser-dialog" role="dialog" aria-modal="true"
        aria-labelledby="web-browser-title">
        <div className="settings-modal-head web-browser-heading">
          <div><h2 id="web-browser-title">网页导入</h2></div>
          <div className="web-browser-heading-actions">
            <button className="button button-secondary" disabled={!connected || saving || paused}
              onClick={() => send({ action: "reload" })} type="button">重新加载</button>
          </div>
          <button ref={closeButton} aria-label="关闭网页导入" disabled={saving}
            onClick={() => void close()} type="button">×</button>
        </div>
        <div className="web-browser-navigation">
          <p className="web-browser-address">{session?.url ?? url}</p>
          <span className={connected && !paused ? "web-browser-connected" : "muted"}
            title={resourceErrors ? "部分图片、字体或其他资源未能加载，网页仍可操作。" : undefined}>
            {paused ? "操作暂停" : connected ? resourceErrors ? "已连接 · 部分内容未加载" : "实时连接"
              : opening ? "正在打开…" : "未连接"}
          </span>
        </div>
        {error ? <div className="web-browser-error" role="alert">
          <span>{error}</span>
          {session && !saving ? <button className="button button-secondary"
            onClick={() => connect(session.id)} type="button">重新连接</button> : null}
        </div> : null}
        <div ref={frame} className="web-browser-frame" aria-busy={opening || !connected}>
          <canvas ref={canvas} aria-label="实时网页，点击后可直接输入和操作" role="img"
            width={1024} height={768} style={{ width: size.width, height: size.height }}
            tabIndex={0} onFocus={() => { if (ready.current) keyboard.current?.focus({ preventScroll: true }); }}
            onPointerDown={(event) => {
              if (!ready.current || event.button !== 0) return;
              const position = point(event);
              if (!position) return;
              event.preventDefault();
              event.currentTarget.setPointerCapture(event.pointerId);
              dragging.current = true;
              lastPoint.current = position;
              keyboard.current?.focus({ preventScroll: true });
              send({ action: "pointer", phase: "down", ...position });
            }}
            onPointerMove={(event) => {
              if (!ready.current) return;
              lastPoint.current = point(event);
              if (moveTimer.current) return;
              moveTimer.current = setTimeout(() => {
                moveTimer.current = null;
                if (lastPoint.current) {
                  send({ action: "pointer", phase: "move", ...lastPoint.current });
                }
              }, 32);
            }}
            onPointerUp={(event) => {
              if (!dragging.current) return;
              dragging.current = false;
              if (moveTimer.current) clearTimeout(moveTimer.current);
              moveTimer.current = null;
              const position = point(event);
              if (position) send({ action: "pointer", phase: "up", ...position });
            }}
            onPointerCancel={releasePointer}
            onLostPointerCapture={releasePointer} />
          {!connected ? <span className="web-browser-loading" role="status">
            {opening ? "正在打开网页…" : error ? "连接已暂停" : "正在连接实时画面…"}
          </span> : null}
          <textarea ref={keyboard} className="web-browser-keyboard" aria-label="网页键盘输入"
            tabIndex={-1} autoComplete="off" autoCapitalize="off" spellCheck={false}
            onKeyDown={(event) => {
              if (!ready.current || event.nativeEvent.isComposing) return;
              if (event.shiftKey && event.key === "Escape") {
                event.preventDefault();
                closeButton.current?.focus();
                return;
              }
              const action = browserKeyAction(event);
              if (action) { event.preventDefault(); send(action); }
            }}
            onCompositionStart={() => { composing.current = true; }}
            onCompositionEnd={(event) => {
              composing.current = false;
              enterText(event.currentTarget.value);
              event.currentTarget.value = "";
            }}
            onChange={(event) => {
              if (!composing.current) {
                enterText(event.currentTarget.value);
                event.currentTarget.value = "";
              }
            }}
            onPaste={(event) => {
              event.preventDefault();
              enterText(event.clipboardData.getData("text/plain"));
            }} />
        </div>
        <div className="web-browser-footer">
          <div className="settings-modal-actions">
            <button className="button button-secondary" disabled={saving}
              onClick={() => void close()} type="button">取消</button>
            <button className="button" disabled={!connected || saving || paused}
              onClick={() => void confirmImport()} type="button">{saving ? "正在处理…" : "确认导入当前页面"}</button>
          </div>
        </div>
      </section>
    </div>
  );
}
