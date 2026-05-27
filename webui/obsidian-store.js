// Alpine store for the Obsidian Canvas surface. On mount it asks the plugin API to start the
// CDP screencast bridge and loads the returned proxied URL into the panel iframe. The bridge's
// own client (inside that iframe) renders the live frames and relays mouse/keyboard/resize —
// the same shape as the Browser surface — so this store only manages the iframe src + status.
import { createStore } from "/js/AlpineStore.js";
import { callJsonApi } from "/js/api.js";

const model = {
  frameSrc: "",
  status: "Starting Obsidian…",
  loading: false,

  async onMount() {
    if (this.frameSrc || this.loading) return;  // idempotent (x-create + register open() may both fire)
    await this.start();
  },

  async start() {
    this.loading = true;
    this.frameSrc = "";
    this.status = "Starting Obsidian…";
    try {
      const res = await callJsonApi("/plugins/obsidian/obsidian_surface", { action: "open" });
      if (res && res.ok && res.url) {
        this.frameSrc = res.url;
        this.status = "";
      } else {
        this.status = (res && res.error) || "Could not start Obsidian. Check the plugin is enabled.";
      }
    } catch (e) {
      this.status = "Could not start Obsidian: " + (e?.message || e);
    } finally {
      this.loading = false;
    }
  },

  cleanup() {
    this.frameSrc = "";
    this.loading = false;
  },
};

export const store = createStore("obsidian", model);
