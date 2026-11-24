/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./demo-store.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Northwind Goods: forest green on warm paper.
        nw: { 50: "#f3f7f4", 100: "#dfeae3", 600: "#2f6b4f", 700: "#24553e", 800: "#1c4231" },
        paper: "#faf7f2",
      },
    },
  },
  plugins: [],
};
