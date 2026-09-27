/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // GulfKloud theme colors (R12.4)
        navy: "#0A1628",
        "navy-dark": "#060F1E",
        mid: "#0D1F35",
        blue: "#2196F3",
        "blue-light": "#E3F2FD",
        white: "#FFFFFF",
        secondary: "#A0AEC0",
        success: "#27AE60",
        warning: "#F5A623",
        error: "#E74C3C",
        card: "#1A2D42",
      },
    },
  },
  plugins: [],
};
