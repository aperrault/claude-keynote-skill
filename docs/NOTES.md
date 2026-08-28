# keynote skill — incident notes and lessons

## Lessons from converting a 530-slide lecture deck's image-math to native equations (2026-08-25)

- **Creating equations in an item that had none used to corrupt the slide**
  (black render, AppleScript -10000 on every item of the slide). Cause: the
  equation template is cloned from *another slide's* component, and its
  `objectReferences` (e.g. image style `9431` in DocumentStylesheet) were never
  declared in the target slide's `externalReferences` in `Metadata.iwa.yaml`.
  Fixed in `keynote_iwa.py` (`IdAllocator.add_external`, called from
  `edit_storage`); it logs `declared external refs [...]` when it acts.
- **12 pt / Helvetica / tight-spacing after text edits — FIXED.** Cause: after an
  edit, paragraph-level tables (`tableParaStyle` etc.) had one entry for several
  paragraphs; Keynote then ignored the table and fell back to defaults (12 pt
  Helvetica, no paragraph spacing). `edit_storage` now rebuilds those tables
  densely (one entry per paragraph, later ones inherit). No post-pass needed.
  Items edited by the old code can be repaired with
  `keynote_iwa.py restyle DECK --slide N --find 'text' [--no-bullets]`.
- **Matching the deck's hand-typed style.** Old slides use paragraph style
  `Body` (HelveticaNeue 48, spaceBefore 59) with list style `None`; a fresh
  layout placeholder uses the same `Body` but a *bulleted* list style. Pass
  `--no-bullets` to `insert-text` / `replace-text` / `restyle` to switch the
  item to the `None` list style. Do not use `keynote.py set-size` on such items
  unless needed — it makes Keynote mint an override style off the default,
  which is what produced the Helvetica look.
- **Placeholders beat `add-text` for math slides.** `add-text` boxes come out
  centered, bold and 12 pt, and alignment is *not* scriptable. To convert an
  image-of-math slide: `add-slide` (Title & Bullets), `set-text` title/body
  with a token like `@@S45@@`, copy notes, `move-slide` into place,
  `delete-slide` the old one, then `replace-text` the token via keynote_iwa.py.
  Bullet-per-paragraph comes from the layout; don't prefix lines with "- ".
- `keynote.py` now has `delete-image`, `delete-slide`, `set-size` and
  `set-geometry` (position/width/height of a text item or image).
  `keynote_iwa.py` has `restyle`, `--no-bullets`, `--eq-size`.
- Keynote's equation renderer: `\;` shows as "; ;" — use `\quad`; `\bigcap_i`
  and `\sum_d` inline are display-sized — use `\cap_i`, `\sum\nolimits_d`.
  A literal backslash in prose (`\nabla` outside `$`) needs no escaping.
- **`move-slide` renumbers as it goes** — moving slide 52 after 62 turns the
  old 53 into 52. Plan moves on the live numbering, and verify with `outline`.
- Pasted images can be recovered from the package: unpack, find the
  `TSD.ImageArchive` on the slide, its `dataReferences` id maps to
  `Data/pasted-image-<id>.png` (name in `Metadata.iwa.yaml`).

## Save-loss incidents (2026-08-25/27) — root cause and fix

- **Keynote's AppleScript `save` returns before the bytes are on disk**, and
  `modified` flips to false immediately. A `close` issued right after (or
  `close ... saving yes`, which does the same internally) cancels the in-flight
  write: mtime unchanged, edits gone. This dropped one of my package edits and
  a whole slide the user had just added. Not OneDrive-specific, but slower
  file-provider paths make the window wider.
- `keynote.py save/close` now: save → **wait until the file's mtime changes**
  (up to 90 s) → confirm `modified` is false → only then close (saving no).
  They print `saved (wrote changes)`; anything else is a failure to act on.
- `keynote_iwa.py` mutators refuse to run while the deck is open in Keynote,
  and the unpack cache is keyed on the file's content hash (never stale).
- Rule of thumb for any edit session: `keynote.py close DECK` → package edits
  → `keynote.py open DECK` → render the touched slides → `keynote.py close`.
  Before editing by slide number, compare `keynote.py outline` and
  `keynote_iwa.py outline` counts: a slide Keynote shows but the file lacks
  means unsaved work is sitting in Keynote.
