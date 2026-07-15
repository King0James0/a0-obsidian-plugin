# Obsidian for Agent Zero

Gives Agent Zero its own [Obsidian](https://obsidian.md/) vault. The plugin installs Obsidian, runs it on a virtual display, and exposes the official `obsidian` CLI so the agent can read, create, search, and organize markdown notes — daily notes, tasks, properties, backlinks — in a persistent, A0-owned vault. It also adds an **Obsidian** surface to the right-side Canvas so you can see the live app (graph view, editor) on demand.

## Support

If this plugin is useful to you, you can support the developer.

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/king0james0) [![Solana](https://img.shields.io/badge/Solana-9945FF?style=for-the-badge&logo=solana&logoColor=white)](https://solscan.io/account/2ZJ2yA6NhM5usgdFRGWw89Z32fzw7Xgga3Eo56jQQiN6) [![Ethereum](https://img.shields.io/badge/Ethereum-3C3C3D?style=for-the-badge&logo=ethereum&logoColor=white)](https://etherscan.io/address/0xfF61681F907fA8DB39C1d23cbdbE89D24A94De17) [![Bitcoin](https://img.shields.io/badge/Bitcoin-F7931A?style=for-the-badge&logo=bitcoin&logoColor=white)](https://mempool.space/address/bc1qyr6x7kmxpy6ke0xutkxg90f30658fnr39h0d7r)

## What it can do

- **Work with notes** (`obsidian-notes` skill) — read/create/append/search notes, append to the daily note, manage tasks, set properties/tags, follow backlinks, all via `obsidian-cli`.
- **Keep a knowledge base** (`obsidian-vault-keeping` skill) — maintain a project-first vault (area folders, hub notes, dated session notes, `[[wikilinks]]`) and log work as it happens, rather than dropping flat notes.
- **See the vault** (`obsidian-canvas-view` skill) — open the **Obsidian** icon in the right-side Canvas rail to view the real Obsidian GUI (graph view, editor, panes), not just the note text.

On first run the plugin seeds a starting structure into the vault (`00-Index/`, `10-Projects/`, `20-Knowledge/`, `30-Research/`, `90-Archive/`, `Daily/`, a `Home` map, and daily-notes config). It's a sensible default — it never overwrites existing notes, and you can restructure however you like.

## Setup

1. **Install** via the Plugin Hub, a GitHub repo URL, or by uploading the plugin ZIP, then enable it.
2. On first run the plugin downloads and installs Obsidian (~85 MB), seeds its config to open an A0-owned vault with the CLI enabled, and launches it headless. **Restart / re-enable** so it comes up.
3. No API key, no manual config. Ask something like *"save a note about X"* or *"what's in my vault about Y?"* and the agent will use `obsidian-cli`.

> Requires a Debian-based image (installs Obsidian via `.deb`), `Xvfb` (present in the standard A0 Docker image), and outbound network on first run.

## How it works

- Obsidian is an Electron desktop app with no headless build, so it runs under a virtual display (`Xvfb`). The CLI talks to that running instance over a unix socket.
- Headless use is enabled by seeding `"cli": true` into Obsidian's `obsidian.json` and registering the vault as open — no GUI interaction needed.
- A `/usr/local/bin/obsidian-cli` wrapper pins `HOME`/`XDG` so the agent's shell reaches the same socket the app opened.
- The Obsidian app stays running (the CLI needs a live instance), so it holds a few hundred MB of RAM — similar to a headless browser.

## Seeing the vault (Obsidian Canvas surface)

The plugin adds its own **Obsidian** icon to Agent Zero's right-side **Canvas** rail (next to Browser, Desktop, and Editor). Click it to stream the **live Obsidian app** — graph view, editor, side panes — right in the canvas. Open Obsidian's graph view from its left ribbon to see your linked notes.

- One Obsidian instance backs both the surface and `obsidian-cli`, so notes you create via the CLI show up live in the surface, and vice-versa.
- **Copy & paste with your own machine.** Select text in the surface and press `Ctrl/Cmd+C` — it lands on *your* local clipboard, so you can paste it into any app on your computer (Windows, macOS, Linux). `Ctrl/Cmd+V` goes the other way: it pastes your local clipboard into the note. The surface is a live screencast of the remote app, so the plugin bridges the clipboard across it for you. (Copying lands on the local clipboard via the async Clipboard API on `https`/`localhost`, falling back to a legacy path when the UI is served over plain `http` on a LAN.)
- The stream starts the first time you open the surface (a few seconds), and is reverse-proxied through Agent Zero — no extra ports are exposed.
- Under the hood it's `xpra shadow --html=on` of Obsidian's display, registered with A0's built-in virtual-desktop gateway (the same machinery as the Desktop surface). Needs `xpra` + `xpra-html5` (present in the standard A0 Docker image).

## Opening notes from the A0 desktop

Right-click a `.md` file on the Agent Zero desktop and choose **Open With Obsidian** (or launch **Obsidian** from the Applications menu) and it opens in the **Obsidian Canvas panel**, showing that note in your live vault.

This is wired through a small launcher the plugin installs (`/usr/local/bin/obsidian-open`) — the system Obsidian launcher is repointed at it. The launcher hands the clicked file to the already-running vault instance (via `obsidian-cli`) and signals the web UI to open the Canvas panel. Files that live outside the vault (e.g. on the desktop) are linked into an `Inbox/` folder in the vault so Obsidian can open them; edits write back to the original.

> Why a launcher? Obsidian is an Electron app that refuses to run as `root` without `--no-sandbox`, so the stock desktop entry silently crashes on the A0 desktop. The launcher avoids that by talking to the running instance instead of starting a new one — and the note shows in the Canvas panel, not as a separate desktop window. (Uninstalling restores the original desktop entry.)

## Where your notes live

The **vault is kept outside the plugin folder** (default `/a0/usr/obsidian`, on the persistent volume) so it **survives uninstall, reinstall, and updates**. Only Obsidian's app config/cache lives inside the plugin (and is cleaned up on uninstall).

## Configuration

`default_config.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `vault_path` | `/a0/usr/obsidian` | Vault location. Point at a mounted host vault to use an existing one. |
| `obsidian_version` | `latest` | Obsidian version to install, or a pinned version like `1.12.7`. |
| `expected_sha256` | `""` | Optional. Pin a version and set the `.deb` SHA-256 to verify the download (empty = HTTPS-only trust). |
| `display` | `:121` | X display Obsidian runs on (one instance serves both the CLI and the Canvas surface). |
| `xpra_port` | `14600` | Loopback TCP port for the Canvas surface's Xpra HTML5 stream (reverse-proxied by A0). |
| `seed_structure` | `true` | Seed the starting folder structure + Home index on first run (never overwrites existing notes). |
| `delete_vault_on_uninstall` | `false` | Safety. Leave false to keep your notes when uninstalling. |

### Download integrity

Obsidian doesn't publish a checksum file, so by default the plugin trusts **HTTPS from the official `obsidianmd` GitHub release** (and `apt`/`dpkg` verify the package's internal integrity). For stronger assurance, pin `obsidian_version` and set `expected_sha256` to that `.deb`'s hash — the plugin then verifies the download and refuses to install on mismatch.

## Uninstalling

Uninstall via the **Plugins UI**: it stops Obsidian, removes the `obsidian-cli` wrapper, and uninstalls Obsidian **only if this plugin installed it**. **Your vault is preserved** (it's your data) — set `delete_vault_on_uninstall: true` only if you really want it deleted.

## Citing

If you use this in your work, please cite it (use the **"Cite this repository"** button on GitHub, or):

```bibtex
@misc{a0obsidianplugin2026,
  title        = {a0-obsidian-plugin: Obsidian vault for Agent Zero},
  author       = {King0James0},
  year         = {2026},
  howpublished = {\url{https://github.com/King0James0/a0-obsidian-plugin}},
  note         = {GitHub repository}
}
```

## License

MIT — see [LICENSE](LICENSE).
