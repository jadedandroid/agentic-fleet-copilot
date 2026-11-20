import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        panel: "#0f172a",
        panelSoft: "#1e293b",
        accent: "#22d3ee"
      }
    }
  },
  plugins: []
};

export default config;
