/** @type {import('tailwindcss').Config} */
export default {
  // `class`, not `media`: the shell's colour scheme has to be switchable at runtime because
  // it must stay in lockstep with ChatKit's `theme.colorScheme`, which does NOT follow
  // prefers-color-scheme on its own.
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
      },
    },
  },
  // No plugins. Everything here is core utilities; a plugin would be a dependency to
  // explain rather than a problem to solve.
  plugins: [],
};
