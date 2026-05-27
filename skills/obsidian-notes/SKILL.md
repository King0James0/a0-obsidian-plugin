---
name: obsidian-notes
description: Read, create, append, search, and organize notes in Agent Zero's Obsidian vault using the obsidian CLI. Use when the user asks to save/write a note, look something up in the vault, add to the daily note, manage tasks, set note properties/tags, or otherwise work with Obsidian notes.
---

# Obsidian notes (obsidian-cli)

The `obsidian` plugin runs Obsidian headless and puts `obsidian-cli` on PATH, wired to A0's
vault. Use it via the terminal (code_execution_tool). It targets a running instance — if a
command says Obsidian isn't reachable, it's still starting (wait a few seconds and retry).

## Common commands

```bash
obsidian-cli read file="My Note"                      # read by name (wikilink-style)
obsidian-cli create name="My Note" content="# Hi" silent   # create (silent = don't steal focus)
obsidian-cli append file="My Note" content="new line"
obsidian-cli search query="topic" limit=10
obsidian-cli daily:append content="- [ ] new task"    # today's daily note
obsidian-cli property:set name="status" value="done" file="My Note"
obsidian-cli tasks todo                                # list open tasks
obsidian-cli tags counts                               # tags by count
obsidian-cli backlinks file="My Note"
```

- `file=<name>` resolves like a wikilink (no path/extension); `path=<folder/note.md>` is exact.
- Quote values with spaces; use `\n` for newline in `content`.
- Run `obsidian-cli help` for the full, version-accurate command list.

## Rules

- **Place notes in the right area folder — never at the vault root.** Decide the folder FIRST
  (see `obsidian-vault-keeping`): project/repo/app work → `10-Projects/<Project>/`; durable
  project-agnostic reference → `20-Knowledge/`; exploration → `30-Research/`. Then create with a
  path into that folder, e.g. `obsidian-cli create path="10-Projects/<Project>/<Name>.md"
  content=...` (run `obsidian-cli help` for exact create flags). The folder in the example is
  illustrative — pick the one that matches the note, don't default to it.
- **Link related notes** with `[[wikilinks]]` and add frontmatter (`tags`, `date`) on create.
- **Follow links to gather context — don't answer from one note in isolation.** A note's
  `[[wikilinks]]` are *references*, not content: reading a note does NOT load the notes it links
  to. When a note points to others and you need fuller context to answer, traverse the graph —
  `obsidian-cli read file="<Linked Note>"` for each relevant outgoing link, and
  `obsidian-cli backlinks file="<Note>"` to find notes that reference this one. Go as many hops as
  the task needs (usually 1), then synthesize across the notes you gathered. If a note explicitly
  defers to a linked note ("see [[X]]"), read X before answering.
- Prefer `create ... silent` so the agent doesn't change the active view unexpectedly.
- Search before creating to avoid duplicate notes.
- To **show** Obsidian visually (graph view, editor) in the right-side Canvas, use the
  `obsidian-canvas-view` skill (the Obsidian icon in the Canvas rail) — `obsidian-cli` here is
  for reading/writing note content.

## Failure handling

- "unable to find Obsidian" / "not enabled" -> the headless app isn't up yet or the plugin
  didn't finish setup; check the Plugins UI and retry shortly. A restart re-runs setup.
