---
name: obsidian-vault-keeping
description: Keep Agent Zero's Obsidian vault clean, structured, and linked. Apply this whenever you save, create, or organize ANY note — choose the right folder, add frontmatter, link related notes, and log project work as dated session notes. Use for every note-saving request, not only explicit "organize the vault" asks.
---

# Obsidian vault-keeping

Default behavior for the vault: **every note is placed in the right area folder, has
frontmatter, and links to related notes.** Never drop notes at the vault root. Use the
`obsidian-notes` skill (obsidian-cli) for the actual reads/writes.

## Vault structure (seeded on install)

```
00-Index/Home.md      vault map / conventions (start here)
10-Projects/<Project>/   project work + dated session notes + a hub note per project
20-Knowledge/         durable notes, references, how-tos
30-Research/          exploration, findings
90-Archive/           retired material
Daily/                daily notes (YYYY-MM-DD)
Templates/            note templates
```

## On every save — do this

1. **Pick the folder** by topic. **Anything tied to a specific project — a GitHub repo, a
   codebase, an app, or ongoing build/task work — is PROJECT work → `10-Projects/<Project>/`**
   (create the sub-folder if it's new). `20-Knowledge/` is ONLY for durable, project-agnostic
   reference (how-tos, concepts, references) — do NOT file project records, repo notes, or build
   logs there. Exploration/findings → `30-Research/`. When the note is about a named repo / app /
   initiative and you're unsure between Project and Knowledge, choose **Project**. Create the note
   *in that folder* (path-based), not at the root.
2. **Add frontmatter** — at least `tags` and `date`; `project` for project notes.
3. **Link it in — bidirectionally.** Add `[[wikilinks]]` to clearly related notes. If the note
   belongs to an overview/hub (or a project), link the hub → the note **and** the note → back to
   its hub — every child note must carry a `[[<Hub>]]` link; never leave hub edges one-directional.
   Make sure the note is reachable (no orphans).
4. **Project work → a dated session note** (`10-Projects/<Project>/YYYY-MM-DD--<slug>.md`) with
   Summary / Key decisions / Changes made, and add a wikilink to it from that project's hub note
   (`10-Projects/<Project>/<Project>.md` — create the hub if missing).

## Rules

- Match an existing folder before inventing a new one; keep the tree shallow and consistent.
- Search the vault before creating, to extend/link an existing note rather than duplicate it.
- Capture decisions and rationale, not just raw output — the vault is for future recall.
- This structure is a sensible default; if the user prefers a different layout, follow theirs.
- **Links are for using, not just building.** When answering from the vault, follow a note's
  `[[wikilinks]]` and backlinks to gather related context rather than reading one note in isolation
  — see the "Follow links to gather context" rule in `obsidian-notes`.
