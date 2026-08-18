/* fidonis-brand: 1 -- vendored verbatim (as of this stamp) into
   Fidonis/qdrant-ingest's docker/tailwind.config.js. A change to the brand
   belongs in the same revision of both interfaces; see qdrant-ingest's
   docs/ui.md. Do not bump only this copy. */
module.exports = {
  content: ["./templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: {
        brand: ['"Manrope"', "sans-serif"],
        body: ['"IBM Plex Sans"', "sans-serif"],
      },
    },
  },
  plugins: [require("daisyui")],
  daisyui: {
    themes: [
      {
        "fidonis-light": {
          primary: "#0a2f4d",
          "primary-content": "#ffffff",
          secondary: "#1b5e8c",
          "secondary-content": "#ffffff",
          accent: "#c8972a",
          "accent-content": "#07070a",
          neutral: "#2a2a35",
          "neutral-content": "#ffffff",
          "base-100": "#ffffff",
          "base-200": "#f6f5f0",
          "base-300": "#efeee7",
          "base-content": "#07070a",
          info: "#3abff8",
          success: "#36d399",
          warning: "#a87820",
          "warning-content": "#07070a",
          error: "#f87272",
          "--rounded-box": "1rem",
          "--rounded-btn": "0.5rem",
          default: true,
        },
      },
      {
        "fidonis-dark": {
          primary: "#c8972a",
          "primary-content": "#07070a",
          secondary: "#1b5e8c",
          "secondary-content": "#ffffff",
          accent: "#a87820",
          "accent-content": "#07070a",
          neutral: "#24374a",
          "neutral-content": "#edeff2",
          "base-100": "#071422",
          "base-200": "#0d1f30",
          "base-300": "#16324a",
          "base-content": "#edeff2",
          info: "#4fa8da",
          "info-content": "#07131f",
          success: "#36d399",
          warning: "#fbbd23",
          "warning-content": "#1a1206",
          error: "#f87272",
          "--rounded-box": "1rem",
          "--rounded-btn": "0.5rem",
          prefersdark: true,
        },
      },
    ],
    logs: false,
  },
}
