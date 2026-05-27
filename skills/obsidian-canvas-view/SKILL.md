---
name: obsidian-canvas-view
description: Show Obsidian's real graphical UI (graph view, note editor, panes) in Agent Zero's right-side Canvas. Use when the user wants to SEE the vault visually, view the knowledge graph, or look at Obsidian itself — not just read/write note text (that's obsidian-notes).
---

# View Obsidian in the Canvas

The `obsidian` plugin adds its own **Obsidian** surface to the right-side Canvas rail (next to
Browser, Desktop, Editor). Opening it streams the live Obsidian app — graph view, editor, panes —
right in the canvas.

## How to show it

Surfaces are opened by the **user** from the UI, not by a tool call. So:

1. Tell the user: open the right-side **Canvas** and click the **Obsidian** icon in the rail.
2. The surface starts the live Obsidian stream (a few seconds the first time) and shows the app.
   To see the linked-notes graph, open Obsidian's **graph view** from its left ribbon.

## Rules

- You cannot click the surface open yourself — guide the user to the **Obsidian** icon in the
  Canvas rail. Don't try to launch Obsidian via the terminal or the Desktop surface for this.
- For reading, creating, or organizing notes, use `obsidian-notes` (the `obsidian-cli`). It works
  whether or not the surface is open, and is the right tool for content changes.
- The same vault backs both the surface and the CLI, so notes you create via `obsidian-cli` show
  up live in the Obsidian surface.
