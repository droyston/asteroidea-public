// The top-bar theme control: one button that cycles System, Light, Dark.
import { nextMode, type ThemeMode, themeStore, useThemeMode } from "./theme";

const LABEL: Record<ThemeMode, string> = { system: "System", light: "Light", dark: "Dark" };

export function ThemeToggle() {
  const mode = useThemeMode();
  return (
    <button
      type="button"
      className="theme-toggle"
      data-theme-mode={mode}
      aria-label={`Theme: ${LABEL[mode]}. Switch to ${LABEL[nextMode(mode)]}`}
      onClick={() => themeStore().cycle()}
    >
      Theme: {LABEL[mode]}
    </button>
  );
}
