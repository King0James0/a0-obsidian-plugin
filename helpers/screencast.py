"""Obsidian Canvas screencast bridge (CDP -> browser), modelled on the Browser surface.

Runs as a standalone process. Connects to a running Obsidian (Electron) over the Chrome DevTools
Protocol, streams the page as JPEG frames to a browser client over a websocket, and relays mouse /
keyboard / resize back into Obsidian via CDP Input + Emulation. Served over plain HTTP+WS so it can
sit behind A0's generic /desktop gateway (HTTP + WS reverse proxy).

Resilience (the whole point of this rewrite): the CDP page session can die or stall during normal
use — a window reload, a heavy WebGL view (graph) under software rendering, or a renderer crash.
The bridge SUPERVISES the CDP connection: when it drops it rediscovers the page target, re-enables
Page + screencast, and resumes pushing to already-connected clients. The browser client likewise
auto-reconnects its websocket. A single click can no longer freeze the surface permanently.

Usage:  python screencast.py --cdp-port 9222 --listen-host 127.0.0.1 --listen-port 14600
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import struct
import subprocess
import time
import urllib.request

import websockets
from aiohttp import web, WSMsgType

CLIENT_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Obsidian</title>
<style>
  html,body{margin:0;height:100%;background:#1e1e1e;overflow:hidden}
  #wrap{position:absolute;inset:0;display:flex;cursor:default}
  #screen{flex:1;width:100%;height:100%;display:block;background:#1e1e1e}
  #msg{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
       color:#b0b0b0;font:14px/1.5 system-ui,sans-serif;pointer-events:none;display:none}
</style></head>
<body>
  <div id="wrap"><img id="screen" draggable="false" alt=""></div>
  <textarea id="pastebuf" aria-hidden="true" tabindex="-1"
    style="position:fixed;left:-9999px;top:0;width:10px;height:10px;opacity:0"></textarea>
  <div id="msg"></div>
<script>
const img = document.getElementById("screen");
const wrap = document.getElementById("wrap");
const msgEl = document.getElementById("msg");
const proto = location.protocol === "https:" ? "wss" : "ws";
// the client is served at .../index.html (or .../) through the gateway; ws lives at .../ws
const base = location.pathname.replace(/\\/index\\.html$/, "/").replace(/\\/$/, "");
const wsUrl = `${proto}://${location.host}${base}/ws`;

let ws = null, natW = 0, natH = 0, gotFrame = false, retry = 0;
function showMsg(t){ msgEl.textContent = t; msgEl.style.display = t ? "block" : "none"; }
function send(o){ if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

// Server->client messages arrive as a BINARY length-prefixed byte stream (the /desktop gateway
// proxy splits/merges large messages, so we reassemble by length prefix, not by message
// boundaries). A 1-byte type tag rides on the channel that's already proven to traverse the
// gateway (we deliberately avoid server->client TEXT frames, which the gateway may not forward):
//   [u32 bodyLen][u8 type][...]
//     type 0 (frame):     [u32 width][u32 height][jpeg bytes]
//     type 1 (clipboard): [utf-8 text]   -- a copy/cut happened in the remote Obsidian
let buf = new Uint8Array(0), lastUrl = null;
function append(chunk){
  const n = new Uint8Array(buf.length + chunk.length);
  n.set(buf, 0); n.set(chunk, buf.length); buf = n;
}
function dv(){ return new DataView(buf.buffer, buf.byteOffset, buf.byteLength); }
function drain(){
  while (buf.length >= 4){
    const bodyLen = dv().getUint32(0);
    if (buf.length < 4 + bodyLen) break;
    const body = buf.subarray(4, 4 + bodyLen);
    const type = body[0];
    if (type === 1){
      // remote clipboard -> push to the local (Windows) clipboard
      writeLocalClipboard(new TextDecoder().decode(body.subarray(1)));
    } else {
      const bdv = new DataView(body.buffer, body.byteOffset + 1, body.byteLength - 1);
      natW = bdv.getUint32(0); natH = bdv.getUint32(4);
      const jpeg = body.subarray(9);
      const url = URL.createObjectURL(new Blob([jpeg], {type:"image/jpeg"}));
      img.src = url;
      if (lastUrl) URL.revokeObjectURL(lastUrl);
      lastUrl = url;
      gotFrame = true; showMsg("");
    }
    buf = buf.subarray(4 + bodyLen);
  }
  // compact so the backing buffer doesn't grow unbounded
  if (buf.byteOffset > 0) buf = buf.slice();
}

// Write text to the LOCAL clipboard. Prefer the async Clipboard API (needs a secure context:
// https or localhost); fall back to a hidden-textarea execCommand("copy") so it also works when
// the A0 web UI is served over plain http on the LAN. The remote Ctrl+C is the user activation
// that authorizes this, and the relay round-trip is fast enough to stay inside its window.
async function writeLocalClipboard(text){
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText){
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch(e){ /* fall through to legacy path */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    img.focus();
  } catch(e){}
}

function connect(){
  showMsg(gotFrame ? "Reconnecting…" : "Connecting…");
  buf = new Uint8Array(0);
  ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => { retry = 0; sendResize(); };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") return;  // (no text frames from server today)
    append(new Uint8Array(e.data)); drain();
  };
  ws.onclose = () => { ws = null; retry = Math.min(retry + 1, 6);
    setTimeout(connect, 300 * retry); showMsg("Reconnecting…"); };
  ws.onerror = () => { try { ws.close(); } catch(e){} };
}

// map a DOM event on the <img> to page CSS pixels in Obsidian
function pt(ev){
  const r = img.getBoundingClientRect();
  const sx = (natW || img.naturalWidth || r.width) / r.width;
  const sy = (natH || img.naturalHeight || r.height) / r.height;
  return { x: Math.round((ev.clientX - r.left) * sx), y: Math.round((ev.clientY - r.top) * sy) };
}
function mods(ev){ return (ev.altKey?1:0)|(ev.ctrlKey?2:0)|(ev.metaKey?4:0)|(ev.shiftKey?8:0); }
const BTN = {0:"left",1:"middle",2:"right"};
const BMASK = {0:1,1:4,2:2};  // CDP `buttons` bitfield: left=1, right=2, middle=4
let buttonsMask = 0;          // held buttons — REQUIRED on mouseMoved so DRAGS register (scrollbars)

let lastMove = 0;
img.addEventListener("mousemove", (ev) => {
  const now = performance.now();
  if (!buttonsMask && now - lastMove < 33) return;  // throttle hover; never throttle a drag
  lastMove = now;
  const p = pt(ev);
  send({type:"mouse", action:"mouseMoved", x:p.x, y:p.y, button:"none", buttons:buttonsMask, modifiers:mods(ev)});
});
img.addEventListener("mousedown", (ev) => { ev.preventDefault(); buttonsMask |= (BMASK[ev.button]||1); const p=pt(ev);
  send({type:"mouse", action:"mousePressed", x:p.x, y:p.y, button:BTN[ev.button]||"left", buttons:buttonsMask, clickCount:ev.detail||1, modifiers:mods(ev)}); });
window.addEventListener("mouseup", (ev) => { buttonsMask &= ~(BMASK[ev.button]||1); const p=pt(ev);
  send({type:"mouse", action:"mouseReleased", x:p.x, y:p.y, button:BTN[ev.button]||"left", buttons:buttonsMask, clickCount:ev.detail||1, modifiers:mods(ev)}); });
img.addEventListener("contextmenu", (ev) => ev.preventDefault());
img.addEventListener("wheel", (ev) => { ev.preventDefault(); const p=pt(ev);
  send({type:"mouse", action:"mouseWheel", x:p.x, y:p.y, button:"none", deltaX:ev.deltaX, deltaY:ev.deltaY, modifiers:mods(ev)}); }, {passive:false});
img.addEventListener("dblclick", (ev) => ev.preventDefault());
img.tabIndex = 0;
img.addEventListener("click", () => img.focus());

const pasteBuf = document.getElementById("pastebuf");
// Paste (local Windows clipboard -> remote Obsidian): let the browser's NATIVE paste drop the
// local clipboard into a hidden textarea (works without the Clipboard API / secure context), then
// relay the captured text to the remote and insert it there. We must NOT forward this Ctrl/Cmd+V
// to the remote (that would paste the remote's own, stale clipboard instead).
pasteBuf.addEventListener("paste", (ev) => {
  const text = ((ev.clipboardData || window.clipboardData) || {}).getData
    ? (ev.clipboardData || window.clipboardData).getData("text") : "";
  if (text) send({type:"paste", text});
  setTimeout(() => { pasteBuf.value = ""; img.focus(); }, 0);
});
pasteBuf.addEventListener("blur", () => { pasteBuf.value = ""; });

window.addEventListener("keydown", (ev) => {
  if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && (ev.key === "v" || ev.key === "V")){
    pasteBuf.value = "";
    pasteBuf.focus();   // redirect the imminent native paste into the hidden textarea
    return;             // don't send to remote, don't preventDefault (let the paste fire)
  }
  send({type:"key", action:"keyDown", key:ev.key, code:ev.code, keyCode:ev.keyCode, modifiers:mods(ev),
        text: (ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey) ? ev.key : ""});
  if (!ev.metaKey && !ev.ctrlKey) ev.preventDefault();
});
window.addEventListener("keyup", (ev) => send({type:"key", action:"keyUp", key:ev.key, code:ev.code, keyCode:ev.keyCode, modifiers:mods(ev)}));

let rt = null;
function sendResize(){
  const r = wrap.getBoundingClientRect();
  const w = Math.max(320, Math.round(r.width)), h = Math.max(240, Math.round(r.height));
  send({type:"resize", width:w, height:h});
}
const ro = new ResizeObserver(() => { if (rt) clearTimeout(rt); rt = setTimeout(sendResize, 150); });
ro.observe(wrap);
window.addEventListener("resize", () => { if (rt) clearTimeout(rt); rt = setTimeout(sendResize, 150); });

connect();
</script>
</body></html>
"""


class CDP:
    """Minimal Chrome DevTools Protocol client over a single page-target websocket."""

    def __init__(self, ws: websockets.WebSocketClientProtocol) -> None:
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.on_frame = None    # callable(data_b64, width, height)
        self.on_binding = None  # callable(name, payload) — Runtime.bindingCalled (clipboard)
        self.closed = asyncio.Event()

    async def call(self, method: str, params: dict | None = None, timeout: float = 8) -> dict:
        self._id += 1
        mid = self._id
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)

    async def reader(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg.get("result") or {})
                elif msg.get("method") == "Page.screencastFrame":
                    p = msg["params"]
                    try:
                        await self.ws.send(json.dumps({
                            "id": -1, "method": "Page.screencastFrameAck",
                            "params": {"sessionId": p["sessionId"]},
                        }))
                    except Exception:
                        pass
                    if self.on_frame:
                        md = p.get("metadata", {})
                        self.on_frame(p["data"], int(md.get("deviceWidth", 0)), int(md.get("deviceHeight", 0)))
                elif msg.get("method") == "Runtime.bindingCalled":
                    p = msg.get("params", {})
                    if self.on_binding:
                        self.on_binding(p.get("name"), p.get("payload"))
        except Exception:
            pass
        finally:
            # connection is gone — fail any in-flight calls and signal the supervisor
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()
            self._pending.clear()
            self.closed.set()

    async def start_screencast(self, max_w: int = 1920, max_h: int = 1080) -> None:
        await self.call("Page.startScreencast", {
            "format": "jpeg", "quality": 70, "maxWidth": max_w, "maxHeight": max_h, "everyNthFrame": 1,
        })

    async def enable_clipboard_relay(self) -> None:
        """Wire up remote-copy -> client relay. A CDP Runtime binding (window.__a0clip) lets page
        JS push back to us; a capture-phase copy/cut listener grabs the current selection at copy
        time and hands it over. Installed on every (re)connect and on every future document so it
        survives Obsidian reloads."""
        await self.call("Runtime.enable")
        await self.call("Runtime.addBinding", {"name": "__a0clip"})
        await self.call("Page.addScriptToEvaluateOnNewDocument", {"source": _CLIP_RELAY_JS})
        await self.call("Runtime.evaluate", {"expression": _CLIP_RELAY_JS})

    async def set_window_size(self, width: int, height: int) -> None:
        # Electron's Browser.getWindowForTarget returns no windowId, so resize the *render viewport*
        # via Emulation instead — the page reflows to it and screencast captures at that size.
        await self.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
            "screenWidth": width, "screenHeight": height,
        })


class Client:
    """One browser viewer. A single dedicated writer task sends only the LATEST frame and drops
    stale ones — never issues concurrent sends on the aiohttp ws (which corrupts the socket and is
    exactly what froze the stream through the slower /desktop gateway proxy)."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self._latest: bytes | None = None
        self._aux: list[bytes] = []   # clipboard records — queued, NEVER dropped
        self._wake = asyncio.Event()
        self._closed = False

    def offer(self, payload: bytes) -> None:
        self._latest = payload  # replace, don't queue — old frames are worthless
        self._wake.set()

    def offer_aux(self, record: bytes) -> None:
        self._aux.append(record)  # clipboard etc. — must not be dropped or coalesced
        self._wake.set()

    async def writer(self) -> None:
        try:
            while not self._closed:
                await self._wake.wait()
                self._wake.clear()
                while self._aux:                       # flush queued records first (never lost)
                    await self.ws.send_bytes(self._aux.pop(0))
                payload, self._latest = self._latest, None
                if payload is not None:
                    await self.ws.send_bytes(payload)  # backpressure lives HERE, single-flight
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True
        self._wake.set()


# Hide Obsidian's own window controls (minimize / maximize / CLOSE) inside the streamed app — this
# is an embedded surface, so a user must not be able to close the app out from under it (Browser /
# Editor surfaces hide their window chrome too). Injected on every (re)connect + on reload.
_HIDE_CHROME_JS = (
    "(function(){var id='a0-hide-winctrl';var s=document.getElementById(id);"
    "if(!s){s=document.createElement('style');s.id=id;"
    "(document.head||document.documentElement).appendChild(s);}"
    "s.textContent='.titlebar-button-container.mod-right{display:none !important;}';})();"
)

# Installed in the remote Obsidian page: on copy/cut, grab the current selection and hand it to the
# bridge via the Runtime binding (read AFTER the event so the app's own copy handler has run). The
# bridge relays it down to the viewer, which writes it to the local clipboard.
_CLIP_RELAY_JS = (
    "(function(){if(window.__a0clipHook)return;window.__a0clipHook=true;"
    "function grab(){try{var t=(window.getSelection&&window.getSelection().toString())||'';"
    "if(t&&window.__a0clip)window.__a0clip(t);}catch(e){}}"
    "document.addEventListener('copy',function(){setTimeout(grab,0);},true);"
    "document.addEventListener('cut',function(){setTimeout(grab,0);},true);})();"
)


def _page_ws_url(cdp_port: int) -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=5) as r:
            targets = json.load(r)
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                return t["webSocketDebuggerUrl"]
    except Exception:
        return None
    return None


class Bridge:
    """Owns the (reconnecting) CDP link and fans frames out to browser clients."""

    def __init__(self, cdp_port: int, relaunch_spec: str | None = None) -> None:
        self.cdp_port = cdp_port
        self.cdp: CDP | None = None
        self.clients: set[Client] = set()
        self.last_frame: bytes | None = None
        self.last_frame_ts = 0.0
        self.size: tuple[int, int] | None = None  # last client-requested render size
        self.relaunch_spec = relaunch_spec       # path to setup.py's relaunch.json (watchdog)
        self._last_relaunch = 0.0
        self._loop = asyncio.get_event_loop()
        self._input_q: asyncio.Queue = asyncio.Queue()

    # --- frame fan-out ---------------------------------------------------------------------
    def _on_frame(self, data: str, w: int, h: int) -> None:
        # Binary length-prefixed record so the xpra-oriented /desktop gateway proxy (which splits
        # large WS messages and forwards each fragment as a whole message) can't corrupt it — the
        # client reassembles by length prefix:  [u32 bodyLen][u32 w][u32 h][jpeg]
        jpeg = base64.b64decode(data)
        body = b"\x00" + struct.pack(">II", w, h) + jpeg  # type 0 = frame
        payload = struct.pack(">I", len(body)) + body
        self.last_frame = payload
        self.last_frame_ts = self._loop.time()
        for c in list(self.clients):
            c.offer(payload)  # each client's writer sends the latest, drops stale

    def _on_clipboard(self, name: str | None, payload) -> None:
        """A copy/cut happened in the remote Obsidian — relay the text to every viewer so it lands
        on their local clipboard. Sent as a tagged binary record (type 1) on the frame channel so
        it can't be dropped/coalesced like frames and rides the gateway-proven path."""
        if name != "__a0clip" or not payload:
            return
        body = b"\x01" + str(payload).encode("utf-8")[:2_000_000]  # type 1 = clipboard
        record = struct.pack(">I", len(body)) + body
        for c in list(self.clients):
            c.offer_aux(record)

    # --- CDP supervision -------------------------------------------------------------------
    def _ensure_app_alive(self) -> None:
        """Watchdog: if Obsidian isn't running (crashed, or a stray quit) and we have a relaunch
        spec, resurrect it. Throttled so we don't spawn repeatedly while it boots."""
        if not self.relaunch_spec or (self._loop.time() - self._last_relaunch) < 12:
            return
        try:
            spec = json.load(open(self.relaunch_spec))
        except Exception:
            return
        match = spec.get("proc_match") or "obsidian --no-sandbox --disable-gpu"
        try:
            running = subprocess.run(["pgrep", "-f", match], capture_output=True).returncode == 0
        except Exception:
            running = True  # can't check -> don't risk a duplicate launch
        if running:
            return
        self._last_relaunch = self._loop.time()
        cfg_dir = spec.get("cfg_dir") or ""
        for lk in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                os.remove(os.path.join(cfg_dir, lk))
            except Exception:
                pass
        try:
            log = open(spec.get("log") or os.devnull, "ab")
            subprocess.Popen(spec["argv"], env=spec.get("env") or None,
                             stdout=log, stderr=log, start_new_session=True,
                             cwd=os.path.dirname(cfg_dir) or None)
            print("[watchdog] relaunched Obsidian", flush=True)
        except Exception as e:
            print(f"[watchdog] relaunch failed: {e}", flush=True)

    async def connect_loop(self) -> None:
        """Forever: (re)discover the Obsidian page target, connect, stream; on drop, retry.
        If the app is gone, the watchdog relaunches it so the surface self-heals."""
        while True:
            ws_url = None
            for i in range(120):  # tolerate Obsidian not being ready / mid-relaunch
                ws_url = _page_ws_url(self.cdp_port)
                if ws_url:
                    break
                if i and i % 8 == 0:   # ~every 4s of no target, make sure the app is alive
                    self._ensure_app_alive()
                await asyncio.sleep(0.5)
            if not ws_url:
                await asyncio.sleep(1.0)
                continue
            try:
                cdp_ws = await websockets.connect(ws_url, max_size=64 * 1024 * 1024, ping_interval=None)
            except Exception:
                await asyncio.sleep(1.0)
                continue
            cdp = CDP(cdp_ws)
            cdp.on_frame = self._on_frame
            cdp.on_binding = self._on_clipboard
            self.cdp = cdp
            reader_task = asyncio.create_task(cdp.reader())
            try:
                await cdp.call("Page.enable")
                # hide window controls now + on any future reload of this page
                await cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": _HIDE_CHROME_JS})
                await cdp.call("Runtime.evaluate", {"expression": _HIDE_CHROME_JS})
                await cdp.enable_clipboard_relay()  # remote copy -> local clipboard relay
                if self.size:  # a fresh page session loses the prior Emulation override
                    await cdp.set_window_size(*self.size)
                await cdp.start_screencast()
            except Exception:
                pass
            await cdp.closed.wait()  # blocks until the CDP socket drops
            self.cdp = None
            reader_task.cancel()
            try:
                await cdp_ws.close()
            except Exception:
                pass
            await asyncio.sleep(0.3)  # brief backoff, then rediscover + reconnect

    async def keepalive(self) -> None:
        """CDP screencast only emits on repaint; idle Obsidian is static. Force a frame ~1/s while
        someone is watching, but only when the view has actually gone quiet (don't pile onto an
        already-busy renderer, e.g. an animating graph view)."""
        while True:
            await asyncio.sleep(1.0)
            cdp = self.cdp
            if cdp and self.clients and (self._loop.time() - self.last_frame_ts) > 1.0:
                try:
                    await cdp.start_screencast()
                except Exception:
                    pass

    # --- input -----------------------------------------------------------------------------
    async def dispatch(self, ev: dict) -> None:
        cdp = self.cdp
        if not cdp:
            return
        t = ev.get("type")
        try:
            if t == "mouse":
                await cdp.call("Input.dispatchMouseEvent", {
                    "type": ev["action"], "x": ev["x"], "y": ev["y"],
                    "button": ev.get("button", "none"), "buttons": ev.get("buttons", 0),
                    "clickCount": ev.get("clickCount", 0),
                    "deltaX": ev.get("deltaX", 0), "deltaY": ev.get("deltaY", 0),
                    "modifiers": ev.get("modifiers", 0),
                }, timeout=5)
            elif t == "key":
                p = {"type": ev["action"], "key": ev.get("key", ""), "code": ev.get("code", ""),
                     "windowsVirtualKeyCode": ev.get("keyCode", 0), "modifiers": ev.get("modifiers", 0)}
                if ev.get("text"):
                    p["text"] = ev["text"]
                await cdp.call("Input.dispatchKeyEvent", p, timeout=5)
            elif t == "paste":
                txt = ev.get("text") or ""
                if txt:
                    await cdp.call("Input.insertText", {"text": txt}, timeout=5)
            elif t == "resize":
                w, h = int(ev["width"]), int(ev["height"])
                self.size = (w, h)
                await cdp.set_window_size(w, h)
        except Exception:
            # a single failed/slow CDP call must never kill the client loop or the bridge
            pass

    def enqueue_input(self, ev: dict) -> None:
        """Hand an input event to the worker WITHOUT blocking the ws receive loop. Each CDP
        Input.dispatch round-trip is ~tens of ms on a heavy page; awaiting them inline let a fast
        wheel burst (incl. no-op over-scroll at the bottom) build a multi-second serial backlog,
        so a direction reversal lagged until the backlog drained."""
        self._input_q.put_nowait(ev)

    async def input_worker(self) -> None:
        """Dispatch queued input, coalescing consecutive high-frequency events so a burst can't
        back up: wheel deltas SUM (down+up cancel → instant reversal); moves keep the latest
        position. Discrete events (clicks/keys/resize) keep their order and are never merged."""
        q = self._input_q
        while True:
            ev = await q.get()
            while (ev.get("type") == "mouse" and ev.get("action") in ("mouseWheel", "mouseMoved")
                   and not q.empty()):
                try:
                    nxt = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt.get("type") == "mouse" and nxt.get("action") == ev["action"]:
                    if ev["action"] == "mouseWheel":  # carry summed scroll onto the latest event
                        nxt["deltaX"] = ev.get("deltaX", 0) + nxt.get("deltaX", 0)
                        nxt["deltaY"] = ev.get("deltaY", 0) + nxt.get("deltaY", 0)
                    ev = nxt
                else:  # not coalescable — flush what we have, then continue from nxt (order kept)
                    await self.dispatch(ev)
                    ev = nxt
            await self.dispatch(ev)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--listen-host", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=14600)
    ap.add_argument("--relaunch-spec", default=None)
    args = ap.parse_args()

    bridge = Bridge(args.cdp_port, relaunch_spec=args.relaunch_spec)
    asyncio.create_task(bridge.connect_loop())
    asyncio.create_task(bridge.keepalive())
    asyncio.create_task(bridge.input_worker())

    async def index(_request):
        return web.Response(text=CLIENT_HTML, content_type="text/html")

    async def ws_handler(request):
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        client = Client(ws)
        bridge.clients.add(client)
        writer_task = asyncio.create_task(client.writer())
        if bridge.last_frame:
            client.offer(bridge.last_frame)  # paint the last frame immediately
        if bridge.cdp:
            try:
                await bridge.cdp.start_screencast()  # force a fresh frame for the new client
            except Exception:
                pass
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    ev = json.loads(msg.data)
                except Exception:
                    continue
                bridge.enqueue_input(ev)
        finally:
            client.close()
            writer_task.cancel()
            bridge.clients.discard(client)
        return ws

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/index.html", index),
        web.get("/ws", ws_handler),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.listen_host, args.listen_port)
    await site.start()
    await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
