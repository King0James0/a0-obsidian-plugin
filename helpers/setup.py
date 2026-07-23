"""Obsidian plugin setup (Agent Zero framework runtime, /opt/venv-a0).

Runs the Obsidian desktop app headless under Xvfb and exposes its `obsidian-cli` so the agent
can read/create/search/manage notes in an A0-owned vault. Headless Obsidian is enabled by
seeding `obsidian.json` with `"cli": true` (no GUI/CDP needed). A `/usr/local/bin/obsidian-cli`
wrapper pins HOME/XDG so the agent shell's CLI reaches the same `~/.obsidian-cli.sock` the app
opens.

Layout (recommended):
  <usr>/obsidian-runtime/       app HOME: obsidian.json, .obsidian-cli.sock, Electron cache
                                (plugin state — OUTSIDE the plugin dir so its constant churn isn't
                                seen by A0's recursive plugin-root watchdog; removed on uninstall)
  <vault_path> (default /a0/usr/obsidian)  the NOTES — user data, OUTSIDE the plugin, preserved
                                on uninstall unless delete_vault_on_uninstall is set.

Best-effort: every entry point logs and returns instead of raising, so a failure can't block
A0 startup. Debian-based image only (installs Obsidian via .deb).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
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
# Desktop launcher: the Obsidian .deb registers /usr/share/applications/obsidian.desktop with
# `Exec=/opt/Obsidian/obsidian %U` — which CRASHES on the A0 desktop (Electron refuses to run as
# root without --no-sandbox), so "Open With Obsidian" / the app-menu entry do nothing. We replace
# its Exec with this wrapper, which routes a clicked .md into the already-running vault instance
# (via obsidian-cli) and signals the web UI to open the Obsidian Canvas panel. Reversed on uninstall.
LAUNCHER_PATH = "/usr/local/bin/obsidian-open"
DESKTOP_FILE = "/usr/share/applications/obsidian.desktop"
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
    # Out of the plugin folder on purpose: A0 watches plugin roots recursively, and the Electron
    # profile (cache, leveldb, logs) under here churns the filesystem constantly. Kept inside the
    # plugin dir, that churn could trip A0's startup watchdog registration into a deadlock. Living
    # under usr/ (persistent volume, NOT a watched plugin root) keeps the app state across restarts;
    # cleanup() removes it on uninstall. (The vault — user notes — is separate, see _vault_path.)
    from helpers import files

    return files.get_abs_path("usr", f"{PLUGIN_NAME}-runtime")


def _migrate_legacy_runtime() -> None:
    """One-time move of a pre-1.1.1 runtime dir from inside the (recursively watched) plugin folder
    to the out-of-tree location, so the Electron profile/app state carries over. Best-effort."""
    try:
        legacy = os.path.join(_plugin_dir(), "runtime")
        new = _runtime_dir()
        if os.path.isdir(legacy) and not os.path.exists(new):
            os.makedirs(os.path.dirname(new), exist_ok=True)
            shutil.move(legacy, new)
            _log(f"migrated runtime dir out of the plugin folder -> {new}")
    except Exception as e:
        _log(f"runtime migration skipped: {e}")


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
    """Build environment for Obsidian process, using allowlist of safe variables."""
    rt = _runtime_dir()
    # Allowlist of environment variables to pass through (excludes unrelated secrets/credentials).
    safe_env_keys = {"PATH", "LANG", "LANGUAGE", "LC_ALL", "TZ", "USER", "LOGNAME"}
    safe_env = {k: v for k, v in os.environ.items() if k in safe_env_keys}
    return {
        **safe_env,
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


def _host_arch() -> str:
    """Return the dpkg architecture (amd64 or arm64/aarch64)."""
    try:
        out = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True, timeout=10)
        arch = out.stdout.strip()
        if arch:
            return arch
    except Exception:
        pass
    import platform
    m = platform.machine().lower()
    if m in ("aarch64", "arm64"):
        return "arm64"
    return "amd64"


def _install_tarball(ver: str, cfg: dict) -> bool:
    """Install Obsidian from the arm64 tar.gz (no .deb published for arm64).
    Downloads obsidian-{ver}-arm64.tar.gz, extracts to /opt/Obsidian/."""
    url = (
        "https://github.com/obsidianmd/obsidian-releases/releases/download/"
        f"v{ver}/obsidian-{ver}-arm64.tar.gz"
    )
    tarball = "/tmp/obsidian_install.tar.gz"
    try:
        _log(f"downloading Obsidian {ver} arm64 tarball (~190MB)...")
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=300) as r, open(tarball, "wb") as f:
            shutil.copyfileobj(r, f)
        _log(f"extracting to /opt/Obsidian/ ...")
        os.makedirs("/opt/Obsidian", exist_ok=True)
        subprocess.run(
            ["tar", "xzf", tarball, "-C", "/opt/Obsidian", "--strip-components=1"],
            capture_output=True, timeout=120,
        )
        for bin_name in ("obsidian", "obsidian-cli"):
            p = os.path.join("/opt/Obsidian", bin_name)
            if os.path.exists(p):
                os.chmod(p, 0o755)
        if not os.path.exists(OBS_APP):
            os.symlink(OBS_APP_BIN, OBS_APP)
    except Exception as e:
        _log(f"Obsidian tarball install error: {e}")
        return False
    finally:
        try:
            os.remove(tarball)
        except Exception:
            pass
    return os.path.exists(OBS_APP_BIN)


def _install_deb(ver: str, cfg: dict) -> bool:
    """Install Obsidian from the amd64 .deb package."""
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
    return os.path.exists(OBS_APP_BIN)


def ensure_obsidian_installed(cfg: dict) -> bool:
    if os.path.exists(OBS_APP_BIN):
        return True
    if not shutil.which("apt-get"):
        _log("apt-get not found — this plugin requires a Debian-based A0 image")
        return False
    ver = _resolve_version(cfg)
    if not ver:
        return False
    arch = _host_arch()
    if arch in ("arm64", "aarch64"):
        _log(f"arm64 architecture detected — using tar.gz install path")
        ok = _install_tarball(ver, cfg)
    else:
        ok = _install_deb(ver, cfg)
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
        # shlex.quote every interpolated value: this is generated shell SOURCE (chmod+exec'd), so a
        # path/display with a quote/space/special char must not break out of its literal.
        f'export HOME={shlex.quote(rt)}\n'
        f'export XDG_CONFIG_HOME={shlex.quote(os.path.join(rt, ".config"))}\n'
        f'export XDG_RUNTIME_DIR={shlex.quote(os.path.join(rt, "run"))}\n'
        f'export DISPLAY={shlex.quote(_display(cfg))}\n'
        f'exec {shlex.quote(OBS_CLI)} "$@"\n'
    )
    try:
        with open(WRAPPER_PATH, "w") as f:
            f.write(cli)
        os.chmod(WRAPPER_PATH, 0o755)
    except Exception as e:
        _log(f"could not write obsidian-cli wrapper: {e}")


# --- desktop launcher (Open With Obsidian → Canvas surface) --------------------------------

def _signal_file() -> str:
    # One-shot flag the launcher drops and the web-UI poller consumes (via the obsidian_surface
    # API) to open the Obsidian Canvas panel. Lives in the runtime dir (persistent, plugin-owned).
    return os.path.join(_runtime_dir(), "surface-open.signal")


def _desktop_backup() -> str:
    return os.path.join(_runtime_dir(), "obsidian.desktop.orig")


# Generated launcher (runs on the A0 desktop, talks to the running instance via obsidian-cli — it
# does NOT spawn Obsidian, so the root/--no-sandbox crash never happens). Tokens are substituted.
_LAUNCHER_TMPL = '''#!/opt/venv-a0/bin/python3
# Auto-generated by the Agent Zero obsidian plugin. Routes the desktop "Open With Obsidian" /
# app-menu launch into the running vault instance + the web-UI Obsidian Canvas panel, instead of
# the .deb's launcher (which crashes as root: Electron needs --no-sandbox).
import json, os, subprocess, sys, time

VAULT = __VAULT__
SIGNAL = __SIGNAL__
CLI = __CLI__
INBOX = "Inbox"


def _vault_rel(path):
    """Vault-relative path for the clicked file. Files outside the vault are symlinked into
    Inbox/ (edits write back to the original; no duplicate) so Obsidian's vault model can open them."""
    ap = os.path.realpath(path)
    if ap == VAULT or ap.startswith(VAULT + os.sep):
        return os.path.relpath(ap, VAULT)
    inbox = os.path.join(VAULT, INBOX)
    os.makedirs(inbox, exist_ok=True)
    base = os.path.basename(ap)
    link = os.path.join(inbox, base)
    if not (os.path.islink(link) and os.path.realpath(link) == ap):
        stem, ext = os.path.splitext(base)
        i = 1
        while os.path.lexists(link) and not (os.path.islink(link) and os.path.realpath(link) == ap):
            link = os.path.join(inbox, stem + "-" + str(i) + ext)
            i += 1
        try:
            os.symlink(ap, link)
        except FileExistsError:
            pass
    return os.path.relpath(link, VAULT)


def main():
    rel = ""
    args = [a for a in sys.argv[1:] if a and not a.startswith("-")]
    path = args[0] if args else ""
    # obsidian:// scheme URIs are handled by the running app itself — only act on real files.
    if path and not path.startswith("obsidian:") and os.path.isfile(path):
        try:
            rel = _vault_rel(path)
            subprocess.run([CLI, "open", "path=" + rel], timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    try:
        with open(SIGNAL, "w") as fh:
            json.dump({"ts": int(time.time()), "path": rel}, fh)
    except Exception:
        pass


if __name__ == "__main__":
    main()
'''


def _render_launcher(cfg: dict) -> str:
    rendered = (_LAUNCHER_TMPL
                .replace("__VAULT__", repr(_vault_path(cfg)))
                .replace("__SIGNAL__", repr(_signal_file()))
                .replace("__CLI__", repr(WRAPPER_PATH)))
    # Force LF: a CRLF in the source file would otherwise break the script's `#!` shebang on Linux.
    return rendered.replace("\r\n", "\n").replace("\r", "\n")


def _patch_desktop_entry() -> None:
    """Point the system Obsidian launcher at our wrapper + register .md as a handled type.
    Idempotent (re-run each boot, no-op once patched); backs up the distro entry once for restore."""
    if not os.path.exists(DESKTOP_FILE):
        return
    try:
        original = open(DESKTOP_FILE).read()
    except Exception as e:
        _log(f"could not read {DESKTOP_FILE}: {e}")
        return
    if f"Exec={LAUNCHER_PATH}" in original:
        return  # already patched
    backup = _desktop_backup()
    try:
        if not os.path.exists(backup):
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            open(backup, "w").write(original)
    except Exception:
        pass
    out = []
    for line in original.splitlines():
        if line.startswith("Exec="):
            out.append(f"Exec={LAUNCHER_PATH} %f")
        elif line.startswith("MimeType="):
            mt = line[len("MimeType="):].strip()
            if mt and not mt.endswith(";"):
                mt += ";"
            for extra in ("text/markdown;", "text/x-markdown;"):
                if extra not in mt:
                    mt += extra
            out.append("MimeType=" + mt)
        else:
            out.append(line)
    try:
        open(DESKTOP_FILE, "w").write("\n".join(out) + "\n")
        _log("patched Obsidian desktop launcher -> obsidian-open")
    except Exception as e:
        _log(f"could not patch {DESKTOP_FILE}: {e}")
        return
    try:  # let file managers pick up the new MimeType
        subprocess.run(["update-desktop-database", "/usr/share/applications"],
                       capture_output=True, timeout=30)
    except Exception:
        pass


def install_desktop_launcher(cfg: dict) -> None:
    """Write /usr/local/bin/obsidian-open and repoint the system Obsidian launcher at it."""
    try:
        with open(LAUNCHER_PATH, "w") as f:
            f.write(_render_launcher(cfg))
        os.chmod(LAUNCHER_PATH, 0o755)
    except Exception as e:
        _log(f"could not write desktop launcher: {e}")
        return
    _patch_desktop_entry()


def _restore_desktop_entry() -> None:
    """Reverse install_desktop_launcher: restore the distro .desktop and remove our wrapper.
    Must run before the runtime dir (which holds the backup) is removed in cleanup()."""
    backup = _desktop_backup()
    try:
        if os.path.exists(backup):
            shutil.copyfile(backup, DESKTOP_FILE)
    except Exception as e:
        _log(f"could not restore {DESKTOP_FILE}: {e}")
    try:
        if os.path.exists(LAUNCHER_PATH):
            os.remove(LAUNCHER_PATH)
    except Exception:
        pass


def consume_open_signal() -> dict | None:
    """Read + clear the one-shot open-panel signal. Returns the payload (incl. vault-relative
    `path`) if a launch is pending, else None. Called by the obsidian_surface API poll action."""
    p = _signal_file()
    try:
        if not os.path.exists(p):
            return None
        data = json.loads(open(p).read() or "{}")
    except Exception:
        data = {}
    try:
        os.remove(p)
    except Exception:
        pass
    return data or {}


def open_note_in_vault(rel_path: str) -> bool:
    """Open a vault-relative note in the running instance via obsidian-cli (idempotent).
    Used by the surface poller to re-assert the file after a cold-start panel open."""
    rel_path = (rel_path or "").lstrip("/")
    if not rel_path:
        return False
    try:
        r = subprocess.run([WRAPPER_PATH, "open", f"path={rel_path}"],
                           capture_output=True, text=True, timeout=15,
                           env=_app_env(_config()))  # least-privilege: no A0 secrets to the CLI
        return r.returncode == 0
    except Exception as e:
        _log(f"open_note failed: {e}")
        return False


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
        rt = _runtime_dir()
        os.makedirs(rt, exist_ok=True)
        log = open(os.path.join(rt, "xvfb.log"), "ab")
        subprocess.Popen(
            ["Xvfb", disp, "-screen", "0", f"{w}x{h}x24"],
            stdout=log, stderr=log, start_new_session=True,
            env=_app_env(cfg),  # least-privilege: Xvfb needs no A0 secrets
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
        os.makedirs(rt, exist_ok=True)
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
    os.makedirs(rt, exist_ok=True)
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
    _migrate_legacy_runtime()
    cfg = _config()
    if not ensure_obsidian_installed(cfg):
        _log("Obsidian unavailable; skipping launch")
        return
    seed_config(cfg)
    seed_structure(cfg)
    ensure_wrapper(cfg)
    install_desktop_launcher(cfg)
    launch_obsidian(cfg)


# --- uninstall -----------------------------------------------------------------------------

def cleanup() -> None:
    """Stop Obsidian/Xvfb, remove the wrapper, uninstall Obsidian if WE installed it.

    The VAULT (user notes) is PRESERVED unless delete_vault_on_uninstall is true. The runtime dir
    (Electron profile/app config) lives at usr/obsidian-runtime and is removed here.
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
    # restore the distro Obsidian launcher + remove our wrapper (reads the backup in the runtime
    # dir, so this MUST run before the runtime dir is removed below).
    _restore_desktop_entry()
    # remove the out-of-tree runtime dir (regenerable app state — Electron profile, logs, socket).
    # The vault (user notes) is separate and handled below.
    try:
        shutil.rmtree(_runtime_dir(), ignore_errors=True)
    except Exception:
        pass
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
