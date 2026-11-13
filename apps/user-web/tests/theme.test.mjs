import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import {
  resolveTheme,
  THEME_INIT_SCRIPT,
  THEME_STORAGE_KEY
} from "../../../packages/web-ui/src/theme-preference.ts";

const css = readFileSync(new URL("../../../packages/web-ui/src/styles.css", import.meta.url), "utf8");
const themeBlocks = [
  css.match(/:root \{([\s\S]*?)\n\}/)[1],
  css.match(/:root\[data-theme="dark"\] \{([\s\S]*?)\n\}/)[1]
];

function tokens(block) {
  return Object.fromEntries([...block.matchAll(/--([\w-]+): (#[\da-f]+);/g)]
    .map(([, name, value]) => [name, value]));
}

function rgb(hex, background = [255, 255, 255]) {
  const channels = [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16));
  const alpha = hex.length === 9 ? parseInt(hex.slice(7), 16) / 255 : 1;
  return channels.map((channel, index) => channel * alpha + background[index] * (1 - alpha));
}

function luminance(channels) {
  return channels.map((channel) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  }).reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0);
}

function contrast(foreground, background) {
  const a = luminance(foreground);
  const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

test("theme preference defaults to the system and respects explicit choices", () => {
  assert.equal(resolveTheme(null, false), "light");
  assert.equal(resolveTheme(null, true), "dark");
  assert.equal(resolveTheme("invalid", true), "dark");
  assert.equal(resolveTheme("light", true), "light");
  assert.equal(resolveTheme("dark", false), "dark");
});

test("pre-render theme script uses the same preference rules, including unavailable storage", () => {
  for (const preference of [null, "invalid", "light", "dark"]) {
    for (const prefersDark of [false, true]) {
      const document = { documentElement: { dataset: {} } };
      runInNewContext(THEME_INIT_SCRIPT, {
        document,
        localStorage: { getItem: (key) => {
          assert.equal(key, THEME_STORAGE_KEY);
          return preference;
        } },
        matchMedia: () => ({ matches: prefersDark })
      });
      assert.equal(document.documentElement.dataset.theme, resolveTheme(preference, prefersDark));
    }
  }
  const document = { documentElement: { dataset: {} } };
  let logged = false;
  runInNewContext(THEME_INIT_SCRIPT, {
    document,
    localStorage: { getItem: () => { throw new Error("Storage denied"); } },
    matchMedia: () => ({ matches: true }),
    console: { warn: () => { logged = true; } }
  });
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.equal(logged, true);
});

test("both themes provide complete color tokens with no hardcoded component colors", () => {
  const light = tokens(themeBlocks[0]);
  const dark = tokens(themeBlocks[1]);
  assert.deepEqual(Object.keys(dark).sort(), Object.keys(light).sort());
  assert.doesNotMatch(css.slice(css.indexOf("\n* {")), /#[\da-f]{3,8}\b/i);
});

test("normal text, primary buttons and semantic messages meet WCAG AA contrast in both themes", () => {
  for (const block of themeBlocks) {
    const palette = tokens(block);
    for (const foreground of ["ink", "text-secondary", "muted", "accent", "accent-strong"]) {
      for (const surface of ["surface", "canvas", "surface-subtle"]) {
        const ratio = contrast(rgb(palette[foreground]), rgb(palette[surface]));
        assert.ok(ratio >= 4.5, `${foreground} on ${surface}: ${ratio.toFixed(2)} < 4.5`);
      }
    }
    assert.ok(contrast(rgb(palette["on-accent"]), rgb(palette.accent)) >= 4.5);
    for (const tone of ["danger", "success", "warning"]) {
      const background = rgb(palette[`${tone}-soft`], rgb(palette.surface));
      assert.ok(contrast(rgb(palette[tone]), background) >= 4.5, `${tone} message contrast`);
    }
  }
});
