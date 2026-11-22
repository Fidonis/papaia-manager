/* fidonis-brand: 3 -- the colours, radii and fonts are the platform's design tokens
   from papaia-kit: its Tailwind preset (dist/tailwind/preset.cjs of the vendored kit
   wheel, copied into the build by the Dockerfile) carries the daisyUI themes with the
   token values. This file keeps no value of its own.

   Surface contract of the themes (in both): base-100 is the raised surface (cards,
   popovers, modals), base-200 the page ground, base-300 border and hover. One meaning
   per accent role: primary is the filled action, secondary navigation, links and focus,
   accent "update available", info/success/warning/error state and nothing else. */
const preset = require("./papaia-kit/preset.cjs");

function kit(name) {
  const entry = preset.daisyui.themes.find((theme) => name in theme);
  if (!entry) throw new Error(`papaia-kit preset has no theme ${name}`);
  return entry[name];
}

module.exports = {
  presets: [preset],
  content: ["./templates/**/*.html"],
  /* The page is the content of the kit's frame. The frame's stylesheet lives in cascade
     layers, and any unlayered rule beats a layered one whatever its specificity, so a
     global reset here would restyle the frame. The manager's own reset is scoped to
     #pk-main instead (tailwind.app.css). */
  corePlugins: { preflight: false },
  plugins: [require("daisyui")],
  daisyui: {
    /* Named after the frame's theme attribute: the kit sets data-theme="light" or
       "dark", and nothing when the person follows the system setting, which the first
       theme (":root") and darkTheme (prefers-color-scheme) then cover. */
    themes: [{ light: kit("fidonis-light") }, { dark: kit("fidonis-dark") }],
    darkTheme: "dark",
    base: false,
    logs: false,
  },
};
