/** @type {import('tailwindcss').Config} */
module.exports = {
  // Scan the templates AND the editor JS bundle source. The file tree, tabs,
  // dropdowns, etc. are built in app/static/src/editor.js and their classes
  // (e.g. `tree-children`, the `hidden` collapse state) never appear in the
  // HTML — without scanning the JS, Tailwind purges those component rules and
  // the `.hidden` utility, so collapsed folders won't hide their contents.
  content: [
    "./app/templates/**/*.html",
    "./app/static/src/**/*.js",
  ],
  theme: {
    extend: {
      // Palette is driven by CSS custom properties (defined in input.css) so the
      // light/dark theme toggle — which swaps the vars on <body> — keeps working
      // while utilities like `bg-bg2`, `text-accent`, `border-border` reference them.
      colors: {
        bg: "var(--bg)",
        bg2: "var(--bg2)",
        bg3: "var(--bg3)",
        border: "var(--border)",
        text: "var(--text)",
        text2: "var(--text2)",
        accent: "var(--accent)",
        green: "var(--green)",
        red: "var(--red)",
        yellow: "var(--yellow)",
        purple: "var(--purple)",
      },
      fontFamily: {
        sans: ['"Rubik"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
