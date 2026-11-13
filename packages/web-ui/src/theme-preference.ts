export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "wiki-theme";

export function resolveTheme(preference: string | null, prefersDark: boolean): Theme {
  return preference === "light" || preference === "dark"
    ? preference
    : prefersDark ? "dark" : "light";
}

export const THEME_INIT_SCRIPT = `
(() => {
  let preference = null;
  try {
    preference = localStorage.getItem("${THEME_STORAGE_KEY}");
  } catch (error) {
    console.warn("无法读取主题偏好。", error);
  }
  document.documentElement.dataset.theme =
    preference === "light" || preference === "dark"
      ? preference
      : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
})();
`;
