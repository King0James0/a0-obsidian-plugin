// Registers the "Obsidian" surface in the right-side Canvas rail (alongside Browser/Desktop/
// Editor). On open it mounts the store (which starts the live Obsidian stream). Mirrors the
// built-in _editor surface registration (importing the store + an open() handler is what
// actually bootstraps the panel — the panel's x-create alone is not sufficient).
import { store as obsidianStore } from "/plugins/obsidian/webui/obsidian-store.js";

function waitForElement(selector, timeoutMs = 10000) {
  const found = document.querySelector(selector);
  if (found) return Promise.resolve(found);
  return new Promise((resolve) => {
    const timeout = globalThis.setTimeout(() => {
      observer.disconnect();
      resolve(document.querySelector(selector));
    }, timeoutMs);
    const observer = new MutationObserver(() => {
      const element = document.querySelector(selector);
      if (!element) return;
      globalThis.clearTimeout(timeout);
      observer.disconnect();
      resolve(element);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  });
}

export default async function registerObsidianSurface(surfaces) {
  surfaces.registerSurface({
    id: "obsidian",
    title: "Obsidian",
    icon: "hub", // material symbol — a node/graph glyph
    order: 40,
    modalPath: "/plugins/obsidian/webui/obsidian-surface.html",
    async open() {
      const panel = await waitForElement('[data-surface-id="obsidian"] .obsidian-panel');
      if (panel) await obsidianStore.onMount?.(panel);
    },
    async close() {
      obsidianStore.cleanup?.();
    },
  });
}
