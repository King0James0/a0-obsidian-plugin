// Registers the "Obsidian" surface in the right-side Canvas rail (alongside Browser/Desktop/
// Editor). On open it mounts the store (which starts the live Obsidian stream). Mirrors the
// built-in _editor surface registration (importing the store + an open() handler is what
// actually bootstraps the panel — the panel's x-create alone is not sufficient).
import { store as obsidianStore } from "/plugins/obsidian/webui/obsidian-store.js";
import { callJsonApi } from "/js/api.js";

// Bridges the A0 desktop to the web UI: when the desktop "Open With Obsidian" launcher (or the
// app-menu entry) fires, it drops a one-shot signal; this poller sees it and opens the Obsidian
// Canvas panel (and re-asserts the clicked note). Without this, a desktop launch can't reach the
// web UI to open the panel — they're separate worlds. Runs once per page (global guard).
function startOpenSignalPoller(surfaces) {
  if (globalThis.__a0ObsidianOpenPoller) return;
  globalThis.__a0ObsidianOpenPoller = true;
  let busy = false;
  globalThis.setInterval(async () => {
    // Skip while the tab is hidden/backgrounded: a UI that isn't on screen has no reason to watch
    // for a desktop "Open With Obsidian" click, and each poll POST otherwise leaks an event-loop
    // socketpair in the framework's async API dispatch — a long-lived idle tab would exhaust fds.
    if (busy || document.visibilityState !== "visible") return;
    busy = true;
    try {
      const res = await callJsonApi("/plugins/obsidian/obsidian_surface", { action: "poll_open" });
      if (res && res.ok && res.open) {
        await surfaces.open("obsidian");  // ensures the live instance + stream are up
        if (res.path) {
          try {
            await callJsonApi("/plugins/obsidian/obsidian_surface", { action: "open_note", path: res.path });
          } catch { /* best-effort: launcher already opened it in the warm case */ }
        }
      }
    } catch { /* transient API error — try again next tick */ } finally {
      busy = false;
    }
  }, 2000);
}

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
  startOpenSignalPoller(surfaces);
}
