/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    container: { center: true, padding: '1rem' },
    extend: {
      colors: {
        bg: 'rgb(var(--bg) / <alpha-value>)',
        surface: 'rgb(var(--surface) / <alpha-value>)',
        surface2: 'rgb(var(--surface2) / <alpha-value>)',
        surface3: 'rgb(var(--surface3) / <alpha-value>)',
        border: 'rgb(var(--border) / <alpha-value>)',
        'border-strong': 'rgb(var(--border-strong) / <alpha-value>)',
        text: 'rgb(var(--text) / <alpha-value>)',
        text2: 'rgb(var(--text2) / <alpha-value>)',
        muted: 'rgb(var(--muted) / <alpha-value>)',
        muted2: 'rgb(var(--muted2) / <alpha-value>)',
        primary: 'rgb(var(--primary) / <alpha-value>)',
        'primary-hover': 'rgb(var(--primary-hover) / <alpha-value>)',
        'on-primary': 'rgb(var(--on-primary) / <alpha-value>)',
        success: 'rgb(var(--success) / <alpha-value>)',
        warning: 'rgb(var(--warning) / <alpha-value>)',
        danger: 'rgb(var(--danger) / <alpha-value>)',
        info: 'rgb(var(--info) / <alpha-value>)',
        'danger-fg': 'rgb(var(--danger-fg) / <alpha-value>)',
        'warning-fg': 'rgb(var(--warning-fg) / <alpha-value>)',
        'success-fg': 'rgb(var(--success-fg) / <alpha-value>)',
        'info-fg': 'rgb(var(--info-fg) / <alpha-value>)',
        accent: 'rgb(var(--accent) / <alpha-value>)',
        'sidebar-bg': 'rgb(var(--sidebar-bg) / <alpha-value>)',
        'sidebar-text': 'rgb(var(--sidebar-text) / <alpha-value>)',
        'sidebar-text-muted': 'rgb(var(--sidebar-text-muted) / <alpha-value>)',
        'sidebar-border': 'rgb(var(--sidebar-border) / <alpha-value>)',
        'sidebar-hover': 'rgb(var(--sidebar-hover) / <alpha-value>)',
        // Chart ink and the categorical series order (lib/chartTheme.ts); series follow the entity, never its rank.
        'chart-grid': 'rgb(var(--chart-grid) / <alpha-value>)',
        'chart-axis': 'rgb(var(--chart-axis) / <alpha-value>)',
        'series-1': 'rgb(var(--series-1) / <alpha-value>)',
        'series-2': 'rgb(var(--series-2) / <alpha-value>)',
        'series-3': 'rgb(var(--series-3) / <alpha-value>)',
        'series-4': 'rgb(var(--series-4) / <alpha-value>)',
        'series-5': 'rgb(var(--series-5) / <alpha-value>)',
        'series-6': 'rgb(var(--series-6) / <alpha-value>)',
        'series-7': 'rgb(var(--series-7) / <alpha-value>)',
        'series-8': 'rgb(var(--series-8) / <alpha-value>)',
      },
      // Inter and JetBrains Mono ship with the dashboard (@fontsource-variable, see styles/fonts.css): an appliance
      // without internet access renders the same type as one with it.
      fontFamily: {
        sans: ['"Inter Variable"', 'Inter', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', 'sans-serif'],
        mono: ['"JetBrains Mono Variable"', '"JetBrains Mono"', 'ui-monospace', 'Menlo', 'Consolas', 'monospace'],
      },
      // One type scale in rem, so the display density (the root size, styles/globals.css) scales every page together.
      // At the default 15px root: 2xs 11.25, xs 12.2, sm 13.1, base 14.1, lg 15.9, xl 18.8, 2xl 22.5, 3xl 28, 4xl 34px.
      // Body copy and tables use xs/sm; 2xs is the floor, for uppercase labels and axis ticks only.
      fontSize: {
        '2xs': ['0.75rem', { lineHeight: '1rem' }],
        xs: ['0.8125rem', { lineHeight: '1.125rem' }],
        sm: ['0.875rem', { lineHeight: '1.25rem' }],
        base: ['0.9375rem', { lineHeight: '1.375rem' }],
        lg: ['1.0625rem', { lineHeight: '1.5rem' }],
        xl: ['1.25rem', { lineHeight: '1.75rem' }],
        '2xl': ['1.5rem', { lineHeight: '2rem' }],
        '3xl': ['1.875rem', { lineHeight: '2.25rem' }],
        '4xl': ['2.25rem', { lineHeight: '2.5rem' }],
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(15 23 38 / 0.04), 0 1px 3px 0 rgb(15 23 38 / 0.04)',
        // Dark panels separate by their border and surface step, not by shadow.
        'card-dark': '0 0 0 0 transparent',
        elevated: '0 4px 6px -1px rgb(0 0 0 / 0.08), 0 2px 4px -2px rgb(0 0 0 / 0.05)',
        glow: '0 0 0 1px rgb(var(--primary) / 0.2), 0 0 20px rgb(var(--primary) / 0.15)',
      },
      spacing: {
        '0.75': '0.1875rem',
        '1.25': '0.3125rem',
        '1.75': '0.4375rem',
      },
    },
  },
  plugins: [],
}
