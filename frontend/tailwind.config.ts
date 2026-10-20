import type { Config } from "tailwindcss";

const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: token("background-primary"),
        panel: token("background-secondary"),
        surface: token("surface"),
        elevated: token("surface-hover"),
        input: token("input"),
        ink: token("text-primary"),
        content: token("text-content"),
        muted: token("text-secondary"),
        subtle: token("text-subtle"),
        line: token("border"),
        accent: { DEFAULT: token("accent-primary"), hover: token("accent-hover"), soft: token("accent-soft"), line: token("accent-line"), text: token("accent-text") },
        secondary: token("accent-secondary"),
        success: { DEFAULT: token("success"), soft: token("success-soft"), line: token("success-line") },
        warning: { DEFAULT: token("warning"), soft: token("warning-soft"), line: token("warning-line") },
        danger: { DEFAULT: token("error"), soft: token("error-soft"), line: token("error-line") },
        warn: token("warning"),
        terminal: token("terminal"),
        "on-accent": token("on-accent")
      },
      fontFamily: {
        sans: ["Inter", "Geist", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", "sans-serif"],
        mono: ["JetBrains Mono", "SFMono-Regular", "Consolas", "Liberation Mono", "monospace"]
      },
      borderRadius: { DEFAULT: "6px", md: "8px", lg: "12px", xl: "16px", "2xl": "16px" },
      transitionDuration: { DEFAULT: "160ms" },
      boxShadow: { sm: "0 2px 8px rgb(0 0 0 / 0.08)", md: "0 8px 24px rgb(0 0 0 / 0.16)", "2xl": "0 24px 80px rgb(0 0 0 / 0.45)" }
    }
  },
  plugins: []
};

export default config;
