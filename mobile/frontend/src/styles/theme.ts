// Vant component tokens, remapped to the "terracotta warm-beige" design
// language (mirrors the raw palette in styles/global.css :root — the two are
// a synced pair; change brand colours in BOTH). Fed to Vant via
// <van-config-provider :theme-vars>. Replaces Vant's default Material blue.
//
// KEY FORMAT (Vant 4): keys are camelCase WITHOUT the `--van-` prefix. Vant's
// mapThemeVarsToCSSVars does `--van-${insertDash(kebabCase(key))}`, so
// `primaryColor` -> `--van-primary-color`, `background2` -> `--van-background-2`.
// Passing full `--van-...` keys produced `--van---van-...` (invalid) and every
// component silently fell back to Vant's Material defaults. Do NOT put custom
// palette / raw tokens here (they'd become `--van-...`); those live at :root
// in styles/global.css.
//
// The provider is mounted with theme-vars-scope="global" (App.vue) so these land
// on <html> — required because Vant teleports popups/dialogs/toasts to <body>,
// outside the provider element, and they must be themed too.

type Vars = Record<string, string>;

const shared: Vars = {
  radiusSm: "12px",
  radiusMd: "16px",
  radiusLg: "22px",
  primaryColor: "#c2683a", // terracotta (global --primary)
  successColor: "#4a8a5e", // --success
  warningColor: "#d68a3a", // --warning
  dangerColor: "#c84a3a", // --danger
  fontSizeMd: "15px",
  cellVerticalPadding: "12px",
  cellHorizontalPadding: "16px",
  navBarHeight: "56px",
  tabbarHeight: "54px",
};

export const lightVars: Vars = {
  ...shared,
  background: "#faf6f1", // --bg
  background2: "#fdfaf6", // --surface (raised)
  textColor: "#2a2118", // --text
  textColor2: "#6b5a4a", // --text-soft
  textColor3: "#9b8b7c", // --text-faint
  borderColor: "#ece2d3", // --outline-soft
  cellBackground: "#fdfaf6",
  navBarBackground: "#faf6f1",
  tabbarBackground: "#faf6f1",
};

export const darkVars: Vars = {
  ...shared,
  primaryColor: "#f0a585", // dark --primary
  background: "#1c1714", // dark --bg
  background2: "#25201c", // dark --surface
  textColor: "#f5ece2", // dark --text
  textColor2: "#c4b4a4",
  textColor3: "#8a7868",
  borderColor: "#36302a", // dark --outline-soft
  cellBackground: "#25201c",
  navBarBackground: "#1c1714",
  tabbarBackground: "#1c1714",
  popupBackground: "#25201c",
  dialogBackground: "#25201c",
};
