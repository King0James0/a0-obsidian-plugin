"""Obsidian plugin setup (Agent Zero framework runtime, /opt/venv-a0).

Runs the Obsidian desktop app headless under Xvfb and exposes its `obsidian-cli` so the agent
can read/create/search/manage notes in an A0-owned vault. Headless Obsidian is enabled by
seeding `obsidian.json` with `"cli": true` (no GUI/CDP needed). A `/usr/local/bin/obsidian-cli`
wrapper pins HOME/XDG so the agent shell's CLI reaches the same `~/.obsidian-cli.sock` the app
opens.

Layout (recommended):
  <plugin>/runtime/             app HOME: obsidian.json, .obsidian-cli.sock, Electron cache
                                (plugin state — removed on uninstall)
  <vault_path> (default /a0/usr/obsidian)  the NOTES — user data, OUTSIDE the plugin, preserved
                                on uninstall unless delete_vault_on_uninstall is set.

Best-effort: every entry point logs and returns instead of raising, so a failure can't block
A0 startup. Debian-based image only (installs Obsidian via .deb).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

PLUGIN_NAME = "obsidian"
INSTALL_MARKER = ".installed-obsidian"          # in plugin dir when WE installed Obsidian
OBS_APP = "/usr/bin/obsidian"                    # launcher symlink -> /opt/Obsidian/obsidian
OBS_APP_BIN = "/opt/Obsidian/obsidian"
OBS_CLI = "/opt/Obsidian/obsidian-cli"
WRAPPER_PATH = "/usr/local/bin/obsidian-cli"
# Matches ONLY the main Obsidian process. It runs as the invoked path `/usr/bin/obsidian
# --no-sandbox --disable-gpu` (a symlink), NOT `Obsidian/obsidian`, and the `--disable-gpu` suffix
# excludes the `--type=zygote` children. Getting this wrong = relaunch-on-every-call + lock churn.
_OBS_PROC = "obsidian --no-sandbox --disable-gpu"
_UA = "agent-zero-obsidian-plugin"
_RELEASES_API = "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest"

# Canvas surface: one Obsidian instance runs on our own X display (with CDP enabled) and serves
# BOTH the CLI (via its HOME socket) and the dedicated "Obsidian" Canvas surface. The surface is
# the live app streamed via CDP screencast by helpers/screencast.py (frames + mouse/keyboard +
# resize, like the Browser surface), reverse-proxied through A0's generic virtual-desktop gateway
# (mounted at /desktop by the built-in _desktop plugin): we register_session() the bridge's port
# and the surface iframes virtual_desktop.session_url(token). See AUTHORING "Showing output".
OBS_TOKEN = "obsidian"  # virtual-desktop session token (also the surface id)


def _log(msg: str) -> None:
    try:
        from helpers.print_style import PrintStyle

        PrintStyle(font_color="cyan").print(f"[{PLUGIN_NAME}] {msg}")
    except Exception:
        print(f"[{PLUGIN_NAME}] {msg}")


def _plugin_dir() -> str:
    from helpers import files

    return files.get_abs_path("usr", "plugins", PLUGIN_NAME)


def _config() -> dict:
    try:
        from helpers import plugins

        cfg = plugins.get_plugin_config(PLUGIN_NAME)
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    try:
        from helpers import files, yaml as yaml_helper

        path = os.path.join(_plugin_dir(), "default_config.yaml")
        if files.exists(path):
            loaded = yaml_helper.loads(files.read_file(path))
            if isinstance(loaded, dict):
                return loaded
    except Exception:
        pass
    return {}


def _runtime_dir() -> str:
    return os.path.join(_plugin_dir(), "runtime")


def _vault_path(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else _config()
    vp = cfg.get("vault_path")
    if vp:
        return vp
    from helpers import files

    return files.get_abs_path("usr", "obsidian")


def _display(cfg: dict | None = None) -> str:
    # Obsidian's own X display (distinct from _desktop :120 and the browser's :99).
    cfg = cfg if cfg is not None else _config()
    return str(cfg.get("display") or ":121")


def _bridge_port(cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else _config()
    try:
        return int(cfg.get("bridge_port") or 14600)
    except Exception:
        return 14600


def _cdp_port(cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else _config()
    try:
        return int(cfg.get("cdp_port") or 9222)
    except Exception:
        return 9222


def _app_env(cfg: dict, display: str | None = None) -> dict:
    rt = _runtime_dir()
    return {
        **os.environ,
        "HOME": rt,
        "XDG_CONFIG_HOME": os.path.join(rt, ".config"),
        "XDG_RUNTIME_DIR": os.path.join(rt, "run"),
        "XDG_CACHE_HOME": os.path.join(rt, ".cache"),
        "DISPLAY": display or _display(cfg),
    }


def _proc_running(pattern: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0
    except Exception:
        return False


# --- install -------------------------------------------------------------------------------

def _resolve_version(cfg: dict) -> str | None:
    ver = str(cfg.get("obsidian_version", "latest")).strip().lstrip("v")
    if ver and ver.lower() != "latest":
        return ver
    try:
        req = urllib.request.Request(_RELEASES_API, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return (json.load(r).get("tag_name") or "").lstrip("v") or None
    except Exception as e:
        _log(f"could not resolve latest Obsidian version: {e}")
        return None


def ensure_obsidian_installed(cfg: dict) -> bool:
    if os.path.exists(OBS_APP_BIN):
        return True
    if not shutil.which("apt-get"):
        _log("apt-get not found — this plugin requires a Debian-based A0 image")
        return False
    ver = _resolve_version(cfg)
    if not ver:
        return False
    url = (
        "https://github.com/obsidianmd/obsidian-releases/releases/download/"
        f"v{ver}/obsidian_{ver}_amd64.deb"
    )
    deb = "/tmp/obsidian_install.deb"
    try:
        _log(f"downloading Obsidian {ver} (~85MB)...")
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=300) as r, open(deb, "wb") as f:
            shutil.copyfileobj(r, f)
        # Integrity: Obsidian publishes no checksum, so the trust anchor is HTTPS + the official
        # obsidianmd release (apt/dpkg also checks package integrity). If the user pinned an
        # expected_sha256, verify it and refuse to install on mismatch.
        expected = str(cfg.get("expected_sha256", "")).strip().lower()
        if expected:
            h = hashlib.sha256()
            with open(deb, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            got = h.hexdigest()
            if got != expected:
                _log(f"SHA-256 mismatch (expected {expected[:12]}…, got {got[:12]}…) — refusing to install")
                return False
            _log("Obsidian .deb SHA-256 verified.")
        else:
            _log("no expected_sha256 set; trusting HTTPS from the official obsidianmd GitHub release.")
        subprocess.run(["apt-get", "update", "-qq"], capture_output=True, timeout=300)
        res = subprocess.run(
            ["apt-get", "install", "-y", deb],
            capture_output=True, text=True, timeout=900,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        if res.returncode != 0:
            _log("apt install failed: " + (res.stderr or res.stdout)[-300:])
    except Exception as e:
        _log(f"Obsidian install error: {e}")
    finally:
        try:
            os.remove(deb)
        except Exception:
            pass
    ok = os.path.exists(OBS_APP_BIN)
    if ok:
        try:
            open(os.path.join(_plugin_dir(), INSTALL_MARKER), "w").write(
                "Obsidian installed by the obsidian plugin\n"
            )
        except Exception:
            pass
    return ok


# --- config + wrapper ----------------------------------------------------------------------

def seed_config(cfg: dict) -> None:
    """Register the vault and enable the CLI by seeding obsidian.json (no GUI needed)."""
    rt = _runtime_dir()
    vault = _vault_path(cfg)
    obs_cfg_dir = os.path.join(rt, ".config", "obsidian")
    for d in (vault, obs_cfg_dir, os.path.join(rt, "run"), os.path.join(rt, ".cache")):
        os.makedirs(d, exist_ok=True)
    try:
        os.chmod(os.path.join(rt, "run"), 0o700)
    except Exception:
        pass
    obsidian_json = os.path.join(obs_cfg_dir, "obsidian.json")
    data = {}
    if os.path.exists(obsidian_json):
        try:
            data = json.loads(open(obsidian_json).read()) or {}
        except Exception:
            data = {}
    data.setdefault("vaults", {})
    data["vaults"]["a0vault"] = {"path": vault, "ts": 1717000000000, "open": True}
    # Frameless: this is an embedded surface, so hide Obsidian's own title bar — otherwise the
    # streamed window shows minimize/maximize/CLOSE controls, and a user clicking close quits the
    # app out from under the surface (Browser/Editor surfaces hide their window chrome for the same
    # reason). The watchdog re-launches if it dies anyway.
    data["frame"] = "hidden"
    data["cli"] = True  # enables obsidian-cli headlessly
    try:
        open(obsidian_json, "w").write(json.dumps(data))
    except Exception as e:
        _log(f"could not seed obsidian.json: {e}")


_HOME_MD = """---
tags: [index]
---
# A0 Vault — Home

Agent Zero's knowledge vault. Keep it tidy and linked.

## Areas
- **10-Projects/** — project work; one sub-folder per project, with dated session notes + a hub note
- **20-Knowledge/** — durable notes, references, how-tos
- **30-Research/** — exploration and findings
- **90-Archive/** — retired material
- **Daily/** — daily notes

## Conventions
- Put each note in the matching area folder — not the vault root.
- Link related notes with `[[wikilinks]]`; add frontmatter (`tags`, `date`).
- For project work, write a dated session note and link it from that project's hub note.
"""

_SESSION_TMPL = """---
date:
project:
tags: []
---
#

## Summary

## Key decisions

## Changes made
"""


def seed_structure(cfg: dict) -> None:
    """Seed a starting folder structure + index into a fresh vault (per-item idempotent —
    never overwrites existing notes). A starting point; users may restructure freely."""
    if str(cfg.get("seed_structure", True)).lower() in ("0", "false", "no"):
        return
    vault = _vault_path(cfg)
    for f in ("00-Index", "10-Projects", "20-Knowledge", "30-Research", "90-Archive",
              "Daily", "Templates"):
        os.makedirs(os.path.join(vault, f), exist_ok=True)
    obs_dir = os.path.join(vault, ".obsidian")
    os.makedirs(obs_dir, exist_ok=True)
    seeds = {
        os.path.join(obs_dir, "daily-notes.json"): json.dumps(
            {"folder": "Daily", "format": "YYYY-MM-DD"}
        ),
        os.path.join(vault, "00-Index", "Home.md"): _HOME_MD,
        os.path.join(vault, "Templates", "Session Note.md"): _SESSION_TMPL,
    }
    for path, content in seeds.items():
        if not os.path.exists(path):
            try:
                open(path, "w").write(content)
            except Exception as e:
                _log(f"could not seed {os.path.basename(path)}: {e}")


def ensure_wrapper(cfg: dict) -> None:
    """Write /usr/local/bin/obsidian-cli that pins HOME/XDG so it reaches the app's socket."""
    rt = _runtime_dir()
    cli = (
        "#!/bin/sh\n"
        "# Auto-generated by the Agent Zero obsidian plugin.\n"
        f'export HOME="{rt}"\n'
        f'export XDG_CONFIG_HOME="{os.path.join(rt, ".config")}"\n'
        f'export XDG_RUNTIME_DIR="{os.path.join(rt, "run")}"\n'
        f'export DISPLAY="{_display(cfg)}"\n'
        f'exec "{OBS_CLI}" "$@"\n'
    )
    try:
        with open(WRAPPER_PATH, "w") as f:
            f.write(cli)
        os.chmod(WRAPPER_PATH, 0o755)
    except Exception as e:
        _log(f"could not write obsidian-cli wrapper: {e}")


# --- launch --------------------------------------------------------------------------------

def _start_xvfb(cfg: dict) -> None:
    """Start the Xvfb display. Obsidian (Electron) needs an X display to run; the screencast is
    captured via CDP (DOM-level), so no window manager or specific size is needed — the render
    size is driven by CDP Emulation to match the canvas panel."""
    disp = _display(cfg)
    if _proc_running(f"Xvfb {disp}"):
        return
    try:
        w, h = _screen_size(cfg)
        log = open(os.path.join(_runtime_dir(), "xvfb.log"), "ab")
        subprocess.Popen(
            ["Xvfb", disp, "-screen", "0", f"{w}x{h}x24"],
            stdout=log, stderr=log, start_new_session=True,
        )
        time.sleep(1.5)
    except Exception as e:
        _log(f"could not start Xvfb: {e}")


def launch_obsidian(cfg: dict) -> None:
    """Launch Obsidian with CDP enabled (idempotent). One instance serves BOTH obsidian-cli (via
    its HOME socket) and the Canvas surface (CDP screencast via the bridge)."""
    if _proc_running(_OBS_PROC):
        return
    if not shutil.which("Xvfb"):
        _log("Xvfb not found — cannot run Obsidian")
        return
    _start_xvfb(cfg)
    try:
        rt = _runtime_dir()
        # Clear stale Electron singleton locks — left behind by an unclean shutdown, they make a
        # relaunched Obsidian hand off and exit immediately (no instance for the CLI/stream).
        cfg_dir = os.path.join(rt, ".config", "obsidian")
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                os.remove(os.path.join(cfg_dir, lock))
            except Exception:
                pass
        log_path = os.path.join(rt, "obsidian.log")
        # --enable-unsafe-swiftshader: the graph view (and other WebGL surfaces) fall back to
        # software WebGL under --disable-gpu; without this flag Chromium deprecates that fallback
        # and the canvas stalls/blanks when the user opens the graph. Trusted local content.
        argv = [OBS_APP, "--no-sandbox", "--disable-gpu", "--enable-unsafe-swiftshader",
                f"--remote-debugging-port={_cdp_port(cfg)}"]
        env = _app_env(cfg)
        # Leave a relaunch spec so the screencast bridge's watchdog can resurrect Obsidian if it
        # dies (crash, or a stray quit) — keeps the surface self-healing.
        _write_relaunch_spec(cfg, argv, env, cfg_dir, log_path)
        log = open(log_path, "ab")
        subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True, cwd=rt)
        _log(f"launched Obsidian on {_display(cfg)} (vault: {_vault_path(cfg)})")
    except Exception as e:
        _log(f"could not launch Obsidian: {e}")


def _relaunch_spec_path() -> str:
    return os.path.join(_runtime_dir(), "relaunch.json")


def _write_relaunch_spec(cfg: dict, argv: list, env: dict, cfg_dir: str, log_path: str) -> None:
    """Record how to relaunch Obsidian so the bridge watchdog can restart it without importing us."""
    try:
        spec = {"argv": argv, "env": {k: str(v) for k, v in env.items()},
                "cfg_dir": cfg_dir, "log": log_path, "proc_match": _OBS_PROC}
        with open(_relaunch_spec_path(), "w") as f:
            json.dump(spec, f)
    except Exception as e:
        _log(f"could not write relaunch spec: {e}")


# --- Canvas surface (live Obsidian via CDP screencast bridge, proxied by A0's /desktop gateway) -

def _screen_size(cfg: dict) -> tuple[int, int]:
    try:
        w, h = str(cfg.get("screen") or "1280x800").lower().split("x")
        return int(w), int(h)
    except Exception:
        return 1280, 800


def _http_ready(port: int, host: str = "127.0.0.1") -> bool:
    """True once the screencast bridge is actually serving HTTP (not just listening) — returning
    the URL too early makes the iframe's first request hit the proxy before the bridge is up."""
    try:
        urllib.request.urlopen(f"http://{host}:{port}/", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _bridge_running(cfg: dict) -> bool:
    return _proc_running(f"screencast.py --cdp-port {_cdp_port(cfg)}")


def start_surface_session(cfg: dict | None = None) -> str | None:
    """Ensure Obsidian (CDP) + the screencast bridge are running, register the bridge with A0's
    virtual-desktop gateway, and return the proxied surface URL the iframe loads. Idempotent.
    Called in the web-server process by the obsidian_surface API when the user opens the surface."""
    cfg = cfg if cfg is not None else _config()
    launch_obsidian(cfg)  # idempotent; the instance the surface streams + the CLI uses
    port = _bridge_port(cfg)
    rt = _runtime_dir()
    if not _bridge_running(cfg):
        try:
            bridge = os.path.join(_plugin_dir(), "helpers", "screencast.py")
            log = open(os.path.join(rt, "screencast.log"), "ab")
            subprocess.Popen(
                [sys.executable, bridge, "--cdp-port", str(_cdp_port(cfg)),
                 "--listen-host", "127.0.0.1", "--listen-port", str(port),
                 "--relaunch-spec", _relaunch_spec_path()],
                stdout=log, stderr=log, start_new_session=True, cwd=rt,
            )
        except Exception as e:
            _log(f"could not start screencast bridge: {e}")
            return None
        for _ in range(60):  # bridge connects to CDP then serves; cold start can take ~10s
            if _http_ready(port):
                break
            time.sleep(0.5)
    try:
        from helpers import virtual_desktop

        virtual_desktop.register_session(
            token=OBS_TOKEN, host="127.0.0.1", port=port, owner="obsidian", title="Obsidian",
        )
        return virtual_desktop.session_url(OBS_TOKEN, title="Obsidian")
    except Exception as e:
        _log(f"could not register the Obsidian surface session: {e}")
        return None


def stop_surface_session(cfg: dict | None = None) -> None:
    """Stop the stream (unregister + kill the bridge). Obsidian keeps running so the CLI works."""
    cfg = cfg if cfg is not None else _config()
    try:
        from helpers import virtual_desktop

        virtual_desktop.unregister_session(OBS_TOKEN)
    except Exception:
        pass
    try:
        subprocess.run(["pkill", "-f", "screencast.py --cdp-port"], capture_output=True, timeout=15)
    except Exception:
        pass


def ensure() -> None:
    """Boot/install routine: install Obsidian, seed config, write wrapper, launch the app.
    The Canvas surface's Xpra stream starts lazily when the user opens the surface."""
    cfg = _config()
    if not ensure_obsidian_installed(cfg):
        _log("Obsidian unavailable; skipping launch")
        return
    seed_config(cfg)
    seed_structure(cfg)
    ensure_wrapper(cfg)
    launch_obsidian(cfg)


# --- uninstall -----------------------------------------------------------------------------

def cleanup() -> None:
    """Stop Obsidian/Xvfb, remove the wrapper, uninstall Obsidian if WE installed it.

    The VAULT (user notes) is PRESERVED unless delete_vault_on_uninstall is true. The plugin
    folder (incl. runtime/app config) is deleted by the framework.
    """
    cfg = _config()
    try:
        from helpers import virtual_desktop

        virtual_desktop.unregister_session(OBS_TOKEN)
    except Exception:
        pass
    for pat in (
        "screencast.py --cdp-port",
        _OBS_PROC,
        f"Xvfb {_display(cfg)}",
    ):
        try:
            subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=15)
        except Exception:
            pass
    try:
        if os.path.exists(WRAPPER_PATH):
            os.remove(WRAPPER_PATH)
    except Exception as e:
        _log(f"could not remove wrapper: {e}")
    marker = os.path.join(_plugin_dir(), INSTALL_MARKER)
    if os.path.exists(marker) and shutil.which("apt-get"):
        try:
            _log("removing Obsidian (installed by this plugin)...")
            # purge (not remove) so no 'rc' dpkg config records are left behind
            subprocess.run(
                ["apt-get", "purge", "-y", "obsidian"],
                capture_output=True, text=True, timeout=300,
                env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
            )
        except Exception as e:
            _log(f"apt remove error: {e}")
        try:
            os.remove(marker)
        except Exception:
            pass
    vault = _vault_path(cfg)
    if str(cfg.get("delete_vault_on_uninstall", "")).lower() in ("1", "true", "yes"):
        try:
            shutil.rmtree(vault, ignore_errors=True)
            _log(f"deleted vault {vault} (delete_vault_on_uninstall set)")
        except Exception as e:
            _log(f"could not delete vault: {e}")
    else:
        _log(f"vault preserved at {vault} (delete it manually if you want it gone)")
