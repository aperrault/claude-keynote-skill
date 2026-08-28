# claude-keynote-skill

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) skill for
reading, rendering and editing Apple Keynote decks on macOS — including the
LaTeX of every equation, which no AppleScript interface exposes.

Two layers, used together:

| | `scripts/keynote.py` | `scripts/keynote_iwa.py` |
|---|---|---|
| mechanism | AppleScript/JXA against the running Keynote | edits the `.key` package directly (via [keynote-parser](https://pypi.org/project/keynote-parser/)) |
| reads | text, presenter notes, layouts, images, geometry; renders slides to JPEG | text **with inline `$latex$`**, every equation's source |
| writes | notes, plain text, text boxes, images, slides (add / move / delete / rebuild), sizes | equation LaTeX (edit and **create**), text that contains math, paragraph/list styles |
| needs Keynote | open (opens it for you) | **closed** — refuses to run otherwise |

Why the package layer: each Keynote equation is a rendered PDF plus its LaTeX
source; Keynote re-typesets from the source on open, so editing math is editing
a string, and new equations need no PDF. Details in `SKILL.md`.

## Install

```bash
git clone https://github.com/<you>/claude-keynote-skill ~/.claude/skills/keynote
```

Requirements: macOS with Keynote (tested on Keynote 15.1.1; keynote-parser
officially supports ≤ 14.5 but round-trips 15.x decks), Python 3.11+, and
[`uv`](https://docs.astral.sh/uv/) (the package layer runs as
`uv run --with keynote-parser --with pyyaml python scripts/keynote_iwa.py …`).
The first AppleScript call prompts for Automation permission.

## Safety

This tool rewrites presentation files, so it is built around not losing work:

- every package edit backs the deck up first; mutating AppleScript commands
  keep a rolling backup (`keynote.py backups DECK` lists them);
- `save`/`close` wait until the bytes are on disk and fail loudly if Keynote
  did not write (Keynote's save is asynchronous — a close right after a save
  can cancel the write);
- package edits refuse to run while Keynote has the deck open;
- `keynote.py verify DECK --slides …` checks every item is readable and every
  slide renders after an edit;
- `python3 tests/run_tests.py` exercises every command on `tests/fixture.key`.

Read `SKILL.md` → "The safe edit loop" before editing a deck you care about.

## Limitations

- Floating (non-inline) equations are read-only.
- Styling a span within a run, charts, builds and master layouts are GUI-only.
- Text-box alignment is not scriptable; new text boxes copy the deck's body
  font/size/color but stay centered.
- Tested on one Keynote version and English-language themes; run the test
  suite on your machine first.

## Development

`python3 tests/run_tests.py` (≈3 minutes; needs Keynote). Incident history and
the reasoning behind the guards: `docs/NOTES.md`.

MIT license.
