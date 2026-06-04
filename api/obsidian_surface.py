"""API for the Obsidian Canvas surface.

The surface (webui/obsidian-surface.html + obsidian-store.js) calls this (POST
/plugins/obsidian/obsidian_surface) when it mounts:
  action=open  -> start Obsidian + the CDP screencast stream, register it with A0's virtual-desktop
                  gateway, and return the proxied {url} the surface iframes.
  action=close -> stop the stream (Obsidian keeps running so obsidian-cli still works).
Runs in the web-server process, so register_session() lands in the gateway's in-process registry.
"""

from __future__ import annotations

from helpers.api import ApiHandler, Request
from usr.plugins.obsidian.helpers import setup


class ObsidianSurface(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict:
        action = str(input.get("action") or "open").lower().strip()
        cfg = setup._config()
        if action == "close":
            setup.stop_surface_session(cfg)
            return {"ok": True, "closed": True}
        if action == "poll_open":
            # Web-UI poll: is a desktop "Open With Obsidian" launch pending? (one-shot)
            sig = setup.consume_open_signal()
            return {"ok": True, "open": bool(sig), "path": (sig or {}).get("path", "")}
        if action == "open_note":
            # Re-assert the clicked note in the running instance (covers a cold-start panel open).
            ok = setup.open_note_in_vault(str(input.get("path") or ""))
            return {"ok": ok}
        try:
            url = setup.start_surface_session(cfg)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not url:
            return {
                "ok": False,
                "error": "Could not start the Obsidian session (is xpra/xpra-html5 installed?).",
            }
        return {"ok": True, "url": url}
