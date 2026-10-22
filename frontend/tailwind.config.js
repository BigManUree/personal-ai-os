/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: 'rgb(var(--bg) / <alpha-value>)',
        surface: 'rgb(var(--surface) / <alpha-value>)',
        elevated: 'rgb(var(--elevated) / <alpha-value>)',
        border: 'rgb(var(--border) / <alpha-value>)',
        fg: 'rgb(var(--fg) / <alpha-value>)',
        'fg-secondary': 'rgb(var(--fg-secondary) / <alpha-value>)',
        'fg-muted': 'rgb(var(--fg-muted) / <alpha-value>)',
        accent: 'rgb(var(--accent) / <alpha-value>)',
        'accent-strong': 'rgb(var(--accent-strong) / <alpha-value>)',
        'accent-soft': 'rgb(var(--accent-soft) / <alpha-value>)',
        seal: 'rgb(var(--seal) / <alpha-value>)',
        'seal-soft': 'rgb(var(--seal-soft) / <alpha-value>)',
        success: 'rgb(var(--success) / <alpha-value>)',
        warning: 'rgb(var(--warning) / <alpha-value>)',
        danger: 'rgb(var(--danger) / <alpha-value>)',
        'input-bg': 'rgb(var(--input-bg) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['-apple-system', 'PingFang SC', 'MiSans', 'HarmonyOS Sans SC', 'Segoe UI', 'Microsoft YaHei', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Consolas', 'monospace'],
      },
      borderRadius: {
        lg: '0.875rem',
        md: '0.75rem',
        sm: '0.625rem',
      },
      boxShadow: {
        shell: '0 0 40px rgb(15 23 42 / 0.06)',
      },
    },
  },
  plugins: [],
}
