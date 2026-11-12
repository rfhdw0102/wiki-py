"use client";

import SliderCaptchaControl, { type VerifyParam } from "rc-slider-captcha";
import { useRef, useState } from "react";
import {
  api,
  type CaptchaChallenge
} from "./api";

export function SliderCaptcha({
  onVerified
}: {
  onVerified: (token: string | null) => void;
}) {
  const challengeRef = useRef<CaptchaChallenge | null>(null);
  const [challenge, setChallenge] = useState<CaptchaChallenge | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function requestChallenge() {
    setError(null);
    onVerified(null);
    try {
      const nextChallenge = await api.captchaChallenge();
      challengeRef.current = nextChallenge;
      setChallenge(nextChallenge);
      return {
        bgUrl: nextChallenge.background_image,
        puzzleUrl: nextChallenge.puzzle_image
      };
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "验证码加载失败。");
      throw cause;
    }
  }

  async function verify(data: VerifyParam) {
    const current = challengeRef.current;
    if (!current) throw new Error("验证码已失效");
    try {
      const result = await api.verifyCaptcha({
        challenge_id: current.challenge_id,
        x: data.x,
        y: data.y,
        duration: data.duration,
        trail: data.trail
      });
      onVerified(result.captcha_token);
      setError(null);
    } catch (cause) {
      onVerified(null);
      setError(cause instanceof Error ? cause.message : "滑块验证失败，请重试。");
      throw cause;
    }
  }

  return (
    <div className="captcha-field">
      <span className="captcha-label">安全验证</span>
      <SliderCaptchaControl
        bgSize={{
          width: challenge?.image_width ?? 320,
          height: challenge?.image_height ?? 160
        }}
        limitErrorCount={5}
        onVerify={verify}
        puzzleSize={{
          width: challenge?.puzzle_width ?? 48,
          height: challenge?.puzzle_height ?? 48,
          left: 0,
          top: challenge?.puzzle_top ?? 0
        }}
        request={requestChallenge}
        tipText={{
          default: "向右拖动滑块完成验证",
          loading: "正在生成验证图…",
          moving: "请将拼图移动到缺口处",
          verifying: "正在验证…",
          success: "验证通过",
          error: "位置不正确，请重试",
          errors: "失败次数过多，点击刷新",
          loadFailed: "加载失败，点击重试"
        }}
      />
      {error ? <span className="captcha-error">{error}</span> : null}
    </div>
  );
}
