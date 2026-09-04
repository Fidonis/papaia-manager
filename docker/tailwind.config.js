/* fidonis-brand: 2 -- vendored verbatim (as of this stamp) into
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
    /* Surface contract, and it inverts the daisyUI convention on purpose --
       in BOTH themes:

         base-100  raised surface   cards, popovers, modals, header, sidebar
         base-200  page ground      <body>
         base-300  border / hover

       Reading it the other way round is what made a card in the light theme
       darker than the page it sat on. The dark theme always worked this way;
       only the light one was inverted. Everything daisyUI paints base-100 by
       itself -- modal-box, inputs -- is a raised surface too, so the two agree.

       One meaning per accent role, so a colour is never decoration:

         primary    filled action        the one solid button on a page
         secondary  navigation, links, focus
         accent     update available
         info/success/warning/error   state, and nothing else

       primary and secondary are both blue in the dark theme. They are told
       apart by form, not hue: filled is an action, tinted is navigation. A
       filled active nav row would collapse the distinction. */
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
          "base-200": "#f3f5f7",
          "base-300": "#e0e5ea",
          /* A cool near-black rather than a pure one. #12161a read nicely on
             its own but cost ~0.9 on every opacity-70 label against the old
             #07070a; this keeps the cast and the contrast. */
          "base-content": "#0b0e12",
          info: "#1b6f9e",
          "info-content": "#ffffff",
          success: "#0d724e",
          "success-content": "#ffffff",
          /* The state colours were picked for badges and then used as body
             copy in service_list.html, where they never passed: the old
             success #36d399 came to 1.76:1 on white, error #f87272 to 2.52
             and warning #a87820 to 3.58. These clear 4.5:1 against both
             base-100 and base-200, which is where they actually appear. */
          warning: "#8a6014",
          "warning-content": "#ffffff",
          error: "#c53c3c",
          "error-content": "#ffffff",
          "--rounded-box": "0.75rem",
          "--rounded-btn": "0.5rem",
          default: true,
        },
      },
      {
        "fidonis-dark": {
          primary: "#2b6fa8",
          "primary-content": "#ffffff",
          secondary: "#4ba5dd",
          "secondary-content": "#08161f",
          accent: "#d1a33c",
          "accent-content": "#151008",
          neutral: "#2f353c",
          "neutral-content": "#c4ccd4",
          "base-100": "#17191c",
          "base-200": "#101214",
          "base-300": "#272c32",
          /* #eef1f4 rather than a dimmer off-white: half the labels in this
             panel are opacity-50, and at #e4e7ea those land on 4.45:1 against
             base-100 -- just under AA, and a hair below what the old navy
             theme managed. This value puts them back at 4.76:1. */
          "base-content": "#eef1f4",
          info: "#4ba5dd",
          "info-content": "#08161f",
          success: "#46c98a",
          "success-content": "#06150e",
          warning: "#e0a63a",
          "warning-content": "#1a1206",
          error: "#ef6a6a",
          "error-content": "#1c0808",
          "--rounded-box": "0.75rem",
          "--rounded-btn": "0.5rem",
          prefersdark: true,
        },
      },
    ],
    logs: false,
  },
}
