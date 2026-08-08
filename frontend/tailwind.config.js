/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        navy: "#0B2545",
        navydeep: "#0E2F5A",
        brandblue: "#1565C0",
        accent: "#00B4D8",
        risklow: "#34D399",
        riskmed: "#FBBF24",
        riskcrit: "#F87171",
        // Retint Tailwind's dark-mode slate scale to the NOC console
        // palette. Devices/Topology/Layout already lean on dark:slate-*
        // classes everywhere, so shifting these hexes carries the new
        // console identity across the whole app without touching
        // thousands of lines of JSX.
        slate: {
          50: "#F8FAFC",
          100: "#E7ECF3",
          200: "#C3CBD8",
          300: "#9AA6B8",
          400: "#7C8697",
          500: "#5B6577",
          600: "#3A4356",
          700: "#1D2532",
          800: "#121826",
          900: "#0E131C",
          950: "#080B10",
        },
        // --- NOC console palette -------------------------------------
        noc: {
          bg: "#080B10",
          panel: "#0E131C",
          panel2: "#121826",
          border: "#1D2532",
          borderlit: "#2A3548",
          text: "#E7ECF3",
          muted: "#7C8697",
          faint: "#4B5567",
          cyan: "#22D3EE",
          violet: "#A78BFA",
          good: "#34D399",
          warn: "#FBBF24",
          crit: "#F87171",
        },
      },
      fontFamily: {
        display: ["'Barlow Condensed'", "sans-serif"],
        sans: ["Inter", "-apple-system", "sans-serif"],
        mono: ["'IBM Plex Mono'", "SFMono-Regular", "monospace"],
      },
    },
  },
  plugins: [],
};1