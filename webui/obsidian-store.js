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

  // --- pop-out state ---------------------------------------------------------------------
  // Concurrency (verified in helpers/screencast.py): the bridge FANS OUT frames to any number
  // of websockets (Bridge.clients is a set; ws_handler adds one Client per socket), but every
  // viewer's input and viewport resize are dispatched into the SAME Obsidian page over CDP —
  // dispatch() issues Input.dispatchMouseEvent / Input.dispatchKeyEvent against one page target,
  // and a "resize" event calls Emulation.setDeviceMetricsOverride on that same target. Two live
  // viewers therefore fight over the viewport and both type into the same window. So popping out
  // is EXCLUSIVE: while the popup is open the docked iframe is unmounted (its websocket closes)
  // and the panel shows a restore line. Closing the popup (or pressing restore) re-mounts it.
  poppedOut: false,
  popWin: null,
  isEmbedded: false,
  _popPoll: null,

  async onMount() {
    // Inside a popped window there is no docked panel to pop out again.
    try { this.isEmbedded = window.opener != null; } catch (e) { this.isEmbedded = false; }
    if (this.frameSrc || this.loading) return;  // idempotent (x-create + register open() may both fire)
    await this.start();
  },

  async start() {
    if (this.poppedOut) return;  // never spin up a second viewer behind the popup
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

  // --- pop out to a real browser window ----------------------------------------------------
  popOut() {
    if (!this.frameSrc || this.poppedOut) return;
    let win = null;
    try {
      // Same-origin bridge URL, so the popup carries the A0 session cookie. Deliberately NOT
      // 'noopener': we need the window handle to restore the docked view when it closes, and
      // the child must keep window.opener so it never offers a nested pop-out. The target is
      // our own screencast client, not third-party content.
      win = window.open(this.frameSrc, "a0-obsidian", "popup,width=1200,height=800");
    } catch (e) {
      win = null;
    }
    if (!win) {
      this.status = "Pop-out blocked — allow pop-ups for this site.";
      return;
    }
    this.popWin = win;
    this.poppedOut = true;   // unmounts the docked iframe -> its websocket closes
    this.status = "";
    this._startPopPoll();
  },

  _startPopPoll() {
    this._stopPopPoll();
    // Simplest reliable restore: the popup is same-origin, so .closed is always readable.
    this._popPoll = globalThis.setInterval(() => {
      const win = this.popWin;
      if (!win || win.closed) this.restoreFromPopout();
    }, 700);
  },

  _stopPopPoll() {
    if (this._popPoll) {
      globalThis.clearInterval(this._popPoll);
      this._popPoll = null;
    }
  },

  async restoreFromPopout(closePopup = false) {
    this._stopPopPoll();
    if (closePopup && this.popWin) {
      try { this.popWin.close(); } catch (e) { /* already gone */ }
    }
    this.popWin = null;
    this.poppedOut = false;  // re-mounts the iframe; the bridge repaints the last frame
    // If the docked surface was closed and re-opened while the popup was live, cleanup()
    // already cleared frameSrc — restart the stream instead of leaving a blank panel.
    if (!this.frameSrc) await this.start();
  },

  cleanup() {
    this.frameSrc = "";
    this.loading = false;
    // If the popup is still live, keep the pop-out state: the docked surface can be closed
    // while the popped window keeps streaming, and a later re-open must NOT start a second
    // viewer against the same CDP page. The poll keeps running and self-clears when it closes.
    if (this.popWin && !this.popWin.closed) return;
    this._stopPopPoll();
    this.popWin = null;
    this.poppedOut = false;
  },
};

export const store = createStore("obsidian", model);
