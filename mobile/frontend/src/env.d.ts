/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

// Standard Vue SFC shim so TS can resolve *.vue imports outside
// vue-tsc's built-in handling (e.g. in test helpers).
declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent<object, object, unknown>;
  export default component;
}
