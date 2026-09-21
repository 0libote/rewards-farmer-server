/** Tailwind config for the Rewards Farmer Server dashboard.
 *
 * Content is a single hand-written `web/index.html` (markup + inline scripts).
 * All dynamically-applied classes are full string literals in that file, so
 * the scanner picks them up with no safelist needed. Previously this config
 * lived inline behind the Tailwind Play CDN; it is now compiled to
 * `web/static/app.css` via `bun run build`.
 *
 * @type {import('tailwindcss').Config}
 */
module.exports = {
  darkMode: "class",
  content: ["./web/index.html"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#ecfdf5",
          500: "#10b981",
          600: "#059669",
          700: "#047857",
        },
      },
    },
  },
};
