---
name: keynote
description: Read, render and edit Apple Keynote (.key) decks on macOS — slide text, presenter notes, rasterized slide images, AND the LaTeX of every equation (read, edit, create). Two layers: AppleScript (keynote.py) for live-document work, and the .key package itself (keynote_iwa.py) for anything involving math. Use whenever a task involves a .key file, a Keynote presentation, or lecture/talk slides on this machine.
---

# Keynote decks

Two tools, two layers. Use both.

| | `scripts/keynote.py` (AppleScript) | `scripts/keynote_iwa.py` (package/IWA) |
|---|---|---|
| sees | text, notes, layouts, images, geometry | text **with inline `$latex$`**, every equation's source |
| edits | notes, plain text items, add/delete text boxes, images, slides, reorder | equation LaTeX, text containing math, create new equations |
| needs Keynote open | yes (opens it for you) | **no** — edits the file; Keynote must NOT have it open |
| speed | outline ~1 s; render ~25 s first, <1 s cached | cold ~30 s (unpack+parse, cached), then <1 s; each edit ≈ 10 s |

```bash
K=~/.claude/skills/keynote/scripts/keynote.py
python3 $K outline DECK.key --slides 40-60 --notes
python3 $K render  DECK.key --slides 45,120        # -> JPEG paths, then Read them
python3 $K set-notes DECK.key --slide 45 --text "…"
python3 $K rebuild-slide DECK.key --slide 51 --title "…" --body "@@S51@@" --credit "credit: X"
python3 $K verify DECK.key --slides 45-52       # after reopening an edited deck
python3 $K add-text / add-image / add-slide / move-slide / delete-slide / delete-item
python3 $K delete-image / set-size / set-geometry / layouts / save / close

I() { uv run --with keynote-parser --with pyyaml python ~/.claude/skills/keynote/scripts/keynote_iwa.py "$@"; }
I outline   DECK.key --slides 96-98 [--notes]      # text with $latex$ spliced in  (deck may be open)
I equations DECK.key --slides 97                   # [k]-numbered LaTeX per slide
I set-equation DECK.key --slide 97 --eq 1 --latex '\nabla_\theta J(\theta) = …'
I insert-text  DECK.key --slide 97 --after 'cheaper?' --text ' So $\Theta(nd)$ suffices.'
I replace-text DECK.key --slide 97 --find 'costs $\Theta(nd^2)$' --replace 'costs $O(nd^2)$'
I replace-text DECK.key --slide 51 --no-bullets --find '@@S51@@' --replace $'line 1 $x$\nline 2'
I restyle      DECK.key --slide 51 --no-bullets --find 'line 1'   # repair para/list styles
```

(Define `I` as a shell function, not a variable — zsh does not word-split `$RUN`.)

## How to actually read a deck

1. **`keynote_iwa.py outline`** is the faithful text view: every text item and
   note, with equations rendered as `$latex$` exactly where they sit. Prefer it
   over `keynote.py outline` for any math-bearing deck — the AppleScript view
   shows equations as `￼` or nothing. Presenter notes (`--notes`) are usually
   the richest source of intent.
2. **`keynote.py render`** is the visual view: plots, pasted screenshots,
   layout. 1200 px wide reads dense equations fine. The first render exports
   the whole deck (~25 s / 500 slides) into `~/Library/Caches/claude-keynote/`,
   keyed on deck mtime+size; later renders are sub-second.

Start narrow: outline the whole deck, then render only the slides that matter.

## How math is stored (why this works)

Each equation is a `TSD.ImageArchive` (a rendered PDF) whose extension
`TSWP.EquationInfoArchive` carries `equation_source_text` (LaTeX), font size
and color; inline ones are anchored in text by a `TSWP.DrawableAttachmentArchive`
at a `characterIndex` (the `￼` char). **Keynote re-typesets every equation from
its source text when it opens the deck** — the PDF is only a cache — so editing
math means editing a string, and new equations need no PDF at all. Verified on
Keynote 15.1.1: edited and freshly created equations render correctly, survive
save + reopen, and AppleScript keeps working on the result.

## Editing rules

- **Never `keynote.py set-text` an item that contains equations.** AppleScript
  `set object text` silently deletes every inline equation in the item. The
  command refuses when it sees `￼` anchors (`--force` overrides). Use
  `keynote_iwa.py replace-text` / `insert-text` for those items.
- `keynote_iwa.py` mutators **write in place** and drop a timestamped backup in
  `~/Library/Caches/claude-keynote/backups/` (path printed on stderr). `--out`
  writes elsewhere instead. **Close the deck in Keynote first** — an open
  Keynote will not see the change and will overwrite it on its next save.
- In `--text` / `--replace`, `$…$` creates an equation; `$$` is a literal
  dollar. Anchors (`--after`, `--before`, `--find`) are matched against the
  `outline` view, so they may include `$latex$` and literal `$`. If an anchor
  occurs more than once on the slide, pass `--nth`.
- Deleting text that contains an equation removes the equation object too;
  inserting text with `\n` creates paragraphs that inherit the style of the
  paragraph they were inserted into.
- `set-equation` addresses by the `[k]` shown by `equations` — re-run it after
  any edit to that slide; numbering is positional.
- After a batch of IWA edits, open the deck in Keynote once and `render` the
  touched slides to confirm. The tool does not do this for you.
- Decks in OneDrive/iCloud sync on every save. For big batches consider
  editing a copy (`--out`) and letting the user diff/merge.

Still GUI-only: styling a span *within* a run, charts, builds, master layouts,
floating (non-inline) equations — rare in practice; they are listed as
`(floating eq)` in `outline` and `equations` and can be read but not edited.

## Quirks and troubleshooting

- **The app may not be named "Keynote."** On some machines it is e.g.
  `/Applications/Keynote Creator Studio.app`; the bundle id is `com.apple.Keynote`
  (older installs: `com.apple.iWork.Keynote`). Always target by bundle id —
  never `application "Keynote"`.
- **keynote-parser** (PyPI) is the decoder. It claims "up to Keynote 14.5" but
  round-trips 15.1.1 decks cleanly (tested on a 530-slide deck: pack → open →
  edit → save). Run it through `uv run --with keynote-parser --with pyyaml`.
- **Geometry is ~300× slower than text** over AppleScript (no bulk accessor):
  `keynote.py outline --geometry` only when coordinates are really needed.
- **Placeholders are listed twice** by AppleScript; `keynote.py outline` dedupes
  but keeps the original `[index]` so addressing stays correct.
- **`make new` must be inside `tell slide N`**; JXA cannot `open`/`export`
  (AppleScript can) — `keynote.py` already splits it that way.
- **Scripting suddenly broken** ("every document doesn't understand count",
  nonsense class names like `unmerge id`)? Stale terminology cache after a
  Keynote upgrade. Remove `$(getconf DARWIN_USER_CACHE_DIR)/com.apple.scriptmanager2.le.cache`
  and the copy under `com.apple.Keynote/`, then quit and relaunch Keynote.
- Slide numbering is identical on both layers and includes skipped slides;
  renders use `skipped slides:true` so image N == slide N.
- Automation permission: the first run may prompt; error -1743 means grant it
  in System Settings → Privacy & Security → Automation.

## Safety nets (all automatic)

- **Backups.** Every package edit backs the deck up first; every mutating
  AppleScript command takes a rolling backup (at most one per 15 min, newest
  30 kept). `keynote.py backups DECK` lists them, `keynote.py backup DECK`
  forces one. Dir: `~/Library/Caches/claude-keynote/backups/`.
- **Verified saves.** `save`/`close` wait for the bytes to land and fail loudly.
  Every mutating command also drops a *pending marker*, so a `close` retried
  after a timed-out save still waits for the write instead of trusting
  Keynote's `modified` flag (which lies once a save has been *issued*).
- **Open-deck guard.** Package mutators refuse to run while Keynote has the deck.
- **`verify`** after reopening: every item readable, every slide renders.
- **Tests.** `python3 tests/run_tests.py` runs every mutator on
  `tests/fixture.key` (mechanical content, no course material) and checks
  text, notes, equations, colors, sizes and the guards. Run it after touching
  the scripts.
- Decks with **no equations** work too: the tool synthesizes the equation
  objects (and the `equation-0-imageStyle` media style) from a built-in template.
- `add-text` copies the deck's body font/size/color (`--raw-style` to skip).

## The safe edit loop (do it in this order, every time)

1. `keynote.py close DECK` — saves, **waits for the bytes to land**, prints
   `saved (wrote changes)`. Package mutators refuse to run while the deck is open.
2. Package edits (`keynote_iwa.py …`). Each one backs the deck up first.
3. `keynote.py open DECK`, then `keynote.py verify DECK --slides …` on every
   slide you touched (reads every item via AppleScript and renders; a black
   slide or an unreadable item fails loudly).
4. `keynote.py close DECK` again before the next package batch.

Converting an image-of-math slide: `rebuild-slide` (new placeholder slide with
title, a token body, copied notes, optional credit; old slide deleted) →
close → `replace-text --no-bullets --find '@@token@@' --replace '…$math$…'` →
open → `verify`. Bodies that overflow: `set-size --item 2 --size 40`.

Incidents, root causes and quirks: see docs/NOTES.md.
