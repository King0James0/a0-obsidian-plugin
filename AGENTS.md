# AGENTS.md — operating contract for `a0-obsidian-plugin`

You are working on an Agent Zero plugin that installs the **Obsidian desktop app** into the A0
container, runs it **headless under Xvfb**, and exposes its `obsidian-cli` + a live Canvas surface.
This plugin manages real system state — it downloads and `apt install`s a `.deb`, writes wrappers to
`/usr/local/bin`, patches a system `.desktop` entry, spawns long-lived Xvfb/Electron/bridge
processes, and owns the user's **vault (their notes)**. A mistake here destroys someone's notes,
leaks a secret onto disk, wedges A0 startup, or exhausts file descriptors. Follow these rules exactly.
They are not suggestions.

## What this plugin is
A self-contained A0 plugin (id `obsidian`, headless-GUI archetype). It runs ONE Obsidian instance on
its own X display that serves both `obsidian-cli` (over a HOME-pinned unix socket) and an **Obsidian
Canvas surface** (the live app streamed via CDP screencast by `helpers/screencast.py`, reverse-proxied
through A0's built-in `/desktop` gateway). Three skills drive note-keeping; a desktop launcher routes
"Open With Obsidian" into the running instance. Publishable, model-agnostic, uninstall-clean.

## HARD INVARIANTS — never violate
1. **The vault is the user's data — PRESERVE it.** The vault lives OUTSIDE the plugin folder
   (`_vault_path()`, default `/a0/usr/obsidian`) so it survives uninstall/reinstall/update. `cleanup()`
   deletes the vault ONLY if `delete_vault_on_uninstall` is explicitly set; the default MUST stay
   `false`. Seeding (`seed_structure`) is per-item idempotent and MUST NEVER overwrite an existing note.
2. **Every entry point is best-effort and MUST NOT raise.** `ensure()`/`setup.*` run in
   `startup_migration/_50` and the install/uninstall hooks — a raised exception blocks A0 startup. Every
   function `_log()`s and returns on failure (Obsidian-unavailable just skips launch). Keep it that way.
3. **ONE Obsidian instance, ever — guard every launch by process match.** `launch_obsidian` /
   `start_surface_session` are idempotent via `_proc_running(_OBS_PROC)`. `_OBS_PROC =
   "obsidian --no-sandbox --disable-gpu"` matches ONLY the main process (the `/usr/bin/obsidian` symlink
   path), excluding `--type=zygote` children. Get this match wrong and you relaunch on every call →
   Singleton-lock churn, the CLI/stream loses its instance. Clear stale `SingletonLock/Socket/Cookie`
   before a relaunch, not during a healthy run.
4. **The runtime dir stays OUT of the watched plugin tree.** Electron's profile (cache, leveldb, logs,
   socket) churns constantly; kept under the plugin root it can deadlock A0's recursive plugin-root
   watchdog. It MUST live at `_runtime_dir()` = `usr/obsidian-runtime` (persistent, unwatched). Never
   move app state back inside the plugin folder.
5. **The Canvas surface poller must NOT leak fds.** `register-obsidian.js`'s `startOpenSignalPoller`
   gates its 2s POST on `document.visibilityState === "visible"` (v1.3.1 fix). A0's async ApiHandler
   dispatch leaks an event-loop socketpair per POST, so an idle/backgrounded tab polling forever
   exhausts fds. Keep the visibility gate; the poller is registered once via a global guard.
6. **Reversible system mutations — `cleanup()` undoes ONLY what we did.** Obsidian is `apt purge`d only
   if the `.installed-obsidian` marker proves WE installed it. The patched `obsidian.desktop` is restored
   from `_desktop_backup()` (backup lives in the runtime dir → restore BEFORE removing it). The
   `obsidian-cli` + `obsidian-open` wrappers are removed. `_patch_desktop_entry` is idempotent
   (no-op once patched) and backs up the distro entry exactly once.
7. **Verify the download before trusting it.** `ensure_obsidian_installed` fetches the `.deb` over HTTPS
   from the official `obsidianmd` GitHub release. If the user pinned `expected_sha256`, verify it and
   **refuse to install on mismatch**. Never relax this to "install anyway"; never invent a checksum.

## Build discipline
- **`setup.py` owns the A0 seam; `screencast.py` is a standalone process.** `helpers/setup.py` and the
  extensions import A0 (`from helpers...`, `usr.plugins.obsidian...`). `screencast.py` runs in its own
  process (stdlib + `websockets` + `aiohttp` only) and must NEVER import the plugin — it relaunches the
  app via the JSON `relaunch.json` spec, not by importing `setup`.
- **Per change:** `py_compile` every `.py` via `/opt/venv-a0/bin/python -m py_compile`; `node --check`
  the JS; keep the README config table in sync with `default_config.yaml`. Bump `plugin.yaml` `version`
  on a release and cut a tagged Release with notes.
- **Keep THIS file current.** Update this AGENTS.md in the SAME change whenever you alter a HARD INVARIANT, a cited path/seam/A0 mechanic, or what this plugin is — a stale contract MISLEADS (worse than none). Routine fixes/features that don't change the contract don't touch it.
- **Validate in a THROWAWAY, never a live instance.** Snapshot/commit the A0 container into an isolated
  one; never mutate a live vault, `/usr/local/bin`, or the system `.desktop`. Verify the surface streams
  and the CLI reaches the socket in a real browser/shell. (The maintainer installs the built artifact.)
- **Opsec (public repo):** no secrets, IPs, internal hostnames, personal email, or local dev paths in
  shipped files. `CLAUDE.md` + `.claude/` are dev-only and gitignored. Commits: single human author,
  GitHub no-reply email, NO AI / `Co-Authored-By` trailers.

## Knowledge map (one source of truth each — never duplicate)
- **Structure + behaviour** (install/launch/surface/uninstall, config keys, how it works): `README.md`.
- **Config** (every key + its default + meaning): `default_config.yaml` (the README table mirrors it).
- **Process** (skill-level usage of the CLI/vault/canvas): `skills/obsidian-{notes,vault-keeping,canvas-view}/`.
- **Design rationale** (the non-obvious WHY): the inline comments in `helpers/setup.py` and
  `helpers/screencast.py` — they record the hard-won gotchas (e.g. why a launcher, why frameless, why
  out-of-tree runtime). Read them before changing those mechanisms.

## Verified A0 mechanics (don't re-derive — confirm against the LIVE instance; versions move constantly)
- Lifecycle: `hooks.py` `install()`/`uninstall()` → `setup.ensure()`/`setup.cleanup()`;
  `extensions/python/startup_migration/_50_obsidian_setup.py` runs `setup.ensure()` every boot
  (idempotent install/seed/launch). No `monologue_*` hooks — the agent uses Obsidian via the shell CLI.
- API: `api/obsidian_surface.py` is an `ApiHandler` at POST `/plugins/obsidian/obsidian_surface`
  (actions `open`/`close`/`poll_open`/`open_note`); it runs in the web-server process so
  `virtual_desktop.register_session()` lands in the in-process gateway registry.
- Canvas surface seam: register via `extensions/webui/right_canvas_register_surfaces/register-obsidian.js`
  + the panel HTML; stream through `helpers/virtual_desktop.{register_session,session_url}` (A0's
  `/desktop` gateway, same machinery as the Browser/Desktop surfaces) — no extra exposed ports.
- `plugins.get_plugin_config(name)` returns the saved `config.json` **OR** defaults — **NEVER merged**.
  To add a key to a live install, `save_plugin_config` the COMPLETE dict or the other keys drop.
- Environment assumptions: Debian-based image (`apt-get`), `Xvfb`, `xpra`/`xpra-html5` present in the
  standard A0 Docker image; Electron refuses to run as root without `--no-sandbox` (hence the launcher).
