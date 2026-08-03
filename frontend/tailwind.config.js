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
        risklow: "#2E7D32",
        riskmed: "#F0A400",
        riskcrit: "#C62828",
      },
    },
  },
  plugins: [],
};1