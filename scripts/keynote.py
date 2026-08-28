#!/usr/bin/env python3
"""Read, render and edit Apple Keynote decks from the command line.

Drives Keynote through AppleScript/JXA. Bulk reads are fast (~50 ms for a
530-slide deck); rendering exports the whole deck once and caches it.

See ../SKILL.md for usage guidance and known Keynote quirks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

APP_ID = "com.apple.Keynote"
APP_ID_FALLBACK = "com.apple.iWork.Keynote"
CACHE_ROOT = Path.home() / "Library" / "Caches" / "claude-keynote"


# --------------------------------------------------------------------------
# osascript plumbing
# --------------------------------------------------------------------------

class KeynoteError(RuntimeError):
    pass


def _run(args: list[str], script: str) -> str:
    proc = subprocess.run(
        args, input=script, capture_output=True, text=True, timeout=600
    )
    if proc.returncode != 0:
        raise KeynoteError((proc.stderr or proc.stdout).strip())
    return proc.stdout.strip()


def applescript(body: str) -> str:
    """Run an AppleScript `tell application id "..."` block."""
    for app_id in (APP_ID, APP_ID_FALLBACK):
        script = f'tell application id "{app_id}"\n{body}\nend tell'
        try:
            return _run(["osascript"], script)
        except KeynoteError as exc:
            if "Can’t get application id" in str(exc) or "-1728" in str(exc):
                continue
            raise
    raise KeynoteError("Keynote not found (tried com.apple.Keynote and com.apple.iWork.Keynote)")


def app_path() -> str:
    """POSIX path to the Keynote bundle (its *name* on disk may vary)."""
    return _run(
        ["osascript"], f'return POSIX path of (path to application id "{APP_ID}")'
    ).rstrip("/")


def jxa(body: str) -> str:
    """Run JXA with `kn` (the app) and `doc` resolution left to the caller."""
    script = f'var APP = {json.dumps(app_path())};\n{body}'
    return _run(["osascript", "-l", "JavaScript"], script)


def esc(s: str) -> str:
    """Escape a Python string for embedding in an AppleScript string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------
# document handling
# --------------------------------------------------------------------------

MUTATING = {"set-text", "set-notes", "add-text", "add-image", "add-slide", "move-slide",
            "delete-item", "delete-image", "delete-slide", "set-size", "set-geometry",
            "rebuild-slide"}
BACKUP_DIR = Path.home() / "Library/Caches/claude-keynote/backups"


def rolling_backup(deck: Path, min_age_s: int = 900, keep: int = 30) -> Path | None:
    """Copy the deck (as it is on disk) to the backup dir unless a backup of it
    newer than `min_age_s` exists. Keeps the newest `keep` per deck. Same dir
    and naming as keynote_iwa.py's per-edit backups, so `backups` lists both."""
    import time
    deck = deck.expanduser().resolve()
    if not deck.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    mine = sorted(BACKUP_DIR.glob(f"{deck.stem}-*{deck.suffix}"), key=lambda p: p.stat().st_mtime)
    if mine and time.time() - mine[-1].stat().st_mtime < min_age_s:
        return None
    dst = BACKUP_DIR / f"{deck.stem}-{time.strftime('%Y%m%d-%H%M%S')}{deck.suffix}"
    shutil.copy2(deck, dst)
    for old in mine[:-keep]:
        old.unlink(missing_ok=True)
    return dst


def open_documents() -> list[str]:
    out = applescript("return name of every document")
    return [n.strip() for n in out.split(",") if n.strip()] if out else []


def ensure_open(deck: Path) -> str:
    """Open `deck` in Keynote if needed; return the document name to address it by."""
    deck = deck.expanduser().resolve()
    if not deck.exists():
        raise KeynoteError(f"no such deck: {deck}")
    name = deck.name
    if name in open_documents():
        return name
    applescript(f'open POSIX file "{esc(str(deck))}"')
    # Opening a large deck is asynchronous; poll briefly.
    import time

    for _ in range(120):
        time.sleep(0.5)
        if name in open_documents():
            return name
        # Some builds return the document object without registering it
        # immediately; keep polling rather than failing fast.
    raise KeynoteError(
        f"{name} did not appear in Keynote's document list. If Keynote is showing a "
        "dialog, dismiss it; if scripting looks broken, see SKILL.md > Troubleshooting."
    )


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

EXTRACT_JXA = r"""
var kn = Application(APP);
var docs = kn.documents;
var doc = null;
for (var i = 0; i < docs.length; i++) { if (docs[i].name() === DOCNAME) doc = docs[i]; }
if (!doc) { throw new Error("document not open: " + DOCNAME); }

// Bulk reads: one Apple event per property for the whole deck.
var notes    = doc.slides.presenterNotes();
var layouts  = doc.slides.baseLayout.name();
var skipped  = doc.slides.skipped();
var tiText   = doc.slides.textItems.objectText();
// Geometry is ~300x slower than text: Keynote has no bulk accessor for the
// inherited iWork-item properties, so each item costs its own Apple event
// (~16 s per property across a 500-slide deck). Only fetch it on request.
var tiPos = null, tiW = null, tiH = null;
if (GEOM) {
  tiPos = doc.slides.textItems.position();
  tiW   = doc.slides.textItems.width();
  tiH   = doc.slides.textItems.height();
}
var shText   = doc.slides.shapes.objectText();
var imgName  = doc.slides.images.fileName();
var imgDesc  = doc.slides.images.description();
var tblCount = doc.slides.tables.length;

var out = { name: doc.name(), width: doc.width(), height: doc.height(), slides: [] };
for (var i = 0; i < notes.length; i++) {
  var items = [], seen = {};
  for (var j = 0; j < tiText[i].length; j++) {
    // Keynote lists each inherited layout placeholder twice, with identical
    // text and geometry. Dedupe, but keep the ORIGINAL index so that it still
    // addresses the right item in set-text / delete-item.
    var p = GEOM ? (tiPos[i][j] || {x: null, y: null}) : {x: null, y: null};
    var key = GEOM
      ? [p.x, p.y, tiW[i][j], tiH[i][j], tiText[i][j]].join("|")
      : tiText[i][j];
    if (seen[key]) continue;
    seen[key] = 1;
    items.push({ index: j + 1, text: tiText[i][j], x: p.x, y: p.y,
                 w: GEOM ? tiW[i][j] : null, h: GEOM ? tiH[i][j] : null });
  }
  out.slides.push({
    n: i + 1,
    layout: layouts[i],
    skipped: skipped[i],
    notes: notes[i],
    textItems: items,
    shapes: (shText[i] || []).filter(function (t) { return t && t.length; }),
    images: (imgName[i] || []).map(function (nm, k) {
      return { file: nm, description: (imgDesc[i] || [])[k] || "" };
    })
  });
}
JSON.stringify(out);
"""


def extract(docname: str, geometry: bool = False) -> dict:
    body = (f"var DOCNAME = {json.dumps(docname)};\n"
            f"var GEOM = {'true' if geometry else 'false'};\n{EXTRACT_JXA}")
    return json.loads(jxa(body))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def parse_range(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(1, total + 1))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return [n for n in out if 1 <= n <= total]


def cache_dir_for(deck: Path) -> Path:
    deck = deck.expanduser().resolve()
    stat = deck.stat()
    key = hashlib.sha1(
        f"{deck}|{stat.st_mtime_ns}|{stat.st_size}".encode()
    ).hexdigest()[:16]
    return CACHE_ROOT / f"{deck.stem}-{key}"


def export_png(docname: str, dest_stem: Path) -> None:
    """Export every slide (including skipped ones, so image N == slide N)."""
    dest_stem.parent.mkdir(parents=True, exist_ok=True)
    applescript(
        f'export document "{esc(docname)}" to POSIX file "{esc(str(dest_stem))}" '
        f"as slide images with properties "
        f"{{image format:PNG, all stages:false, skipped slides:true}}"
    )


def downscale(src: Path, dst: Path, width: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("magick"):
        subprocess.run(
            ["magick", str(src), "-resize", f"{width}x", "-quality", "82", str(dst)],
            check=True, capture_output=True,
        )
    else:  # sips ships with macOS
        subprocess.run(
            ["sips", "-Z", str(width), "--setProperty", "format", "jpeg",
             str(src), "--out", str(dst)],
            check=True, capture_output=True,
        )


def render(deck: Path, slides: str | None, width: int, out_dir: Path | None,
           refresh: bool) -> list[Path]:
    docname = ensure_open(deck)
    cache = cache_dir_for(deck)
    raw = cache / "png"
    if refresh and cache.exists():
        shutil.rmtree(cache)
    # Keynote's image export creates a DIRECTORY named after the destination
    # stem, holding <stem>.001.png, <stem>.002.png, ...
    shots = raw / "slide"
    if not shots.exists() or not any(shots.glob("*.png")):
        raw.mkdir(parents=True, exist_ok=True)
        export_png(docname, shots)
    pngs = sorted(shots.glob("slide.*.png"))
    if not pngs:
        raise KeynoteError(f"export produced no images in {raw}")
    wanted = parse_range(slides, len(pngs))
    dest = (out_dir or (cache / f"jpg{width}")).expanduser()
    made = []
    for n in wanted:
        src = shots / f"slide.{n:03d}.png"
        if not src.exists():
            continue
        dst = dest / f"slide-{n:03d}.jpg"
        if not dst.exists():
            downscale(src, dst, width)
        made.append(dst)
    return made


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def to_markdown(data: dict, want: list[int], notes: bool) -> str:
    lines = [f"# {data['name']} — {len(data['slides'])} slides "
             f"({data['width']}x{data['height']})", ""]
    for s in data["slides"]:
        if s["n"] not in want:
            continue
        flag = " [SKIPPED]" if s["skipped"] else ""
        lines.append(f"## Slide {s['n']} — {s['layout']}{flag}")
        for it in s["textItems"]:
            body = (it["text"] or "").strip()
            if not body or body == "￼":
                continue
            first, *rest = body.split("\n")
            lines.append(f"- [{it['index']}] {first}")
            for r in rest:
                if r.strip():
                    lines.append(f"      {r}")
        for sh in s["shapes"]:
            lines.append(f"- (shape) {sh.strip()}")
        for im in s["images"]:
            desc = f" — {im['description']}" if im["description"] else ""
            lines.append(f"- (image) {im['file']}{desc}")
        if notes and s["notes"].strip():
            lines.append("")
            lines.append("  NOTES: " + textwrap.indent(
                s["notes"].strip(), "  ").lstrip())
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(prog="keynote.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def deck_arg(sp):
        sp.add_argument("deck", type=Path, help="path to the .key file")

    sp = sub.add_parser("list", help="list open Keynote documents")

    sp = sub.add_parser("open", help="open a deck in Keynote")
    deck_arg(sp)

    sp = sub.add_parser("outline", help="dump deck text/notes as markdown or JSON")
    deck_arg(sp)
    sp.add_argument("--slides", help="range, e.g. 1-20,45,60-70")
    sp.add_argument("--notes", action="store_true", help="include presenter notes")
    sp.add_argument("--json", action="store_true", help="emit raw JSON")
    sp.add_argument("--geometry", action="store_true",
                    help="include x/y/w/h per item (SLOW: ~50 s on a 500-slide deck)")

    sp = sub.add_parser("render", help="render slides to JPEGs for viewing")
    deck_arg(sp)
    sp.add_argument("--slides", help="range, e.g. 1-20,45")
    sp.add_argument("--width", type=int, default=1200, help="pixel width (default 1200)")
    sp.add_argument("--out", type=Path, help="output directory (default: cache)")
    sp.add_argument("--refresh", action="store_true", help="discard cached export")

    sp = sub.add_parser("set-text", help="replace the text of one text item")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)
    sp.add_argument("--item", type=int, required=True, help="index from `outline`")
    sp.add_argument("--text", required=True)
    sp.add_argument("--force", action="store_true",
                    help="overwrite even if the item contains inline equations (destroys them)")

    sp = sub.add_parser("set-notes", help="replace a slide's presenter notes")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)
    sp.add_argument("--text", required=True)

    sp = sub.add_parser("add-text", help="add a new text box to a slide")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)
    sp.add_argument("--text", required=True)
    sp.add_argument("--x", type=int, default=100)
    sp.add_argument("--y", type=int, default=900)
    sp.add_argument("--width", type=int, default=800)
    sp.add_argument("--raw-style", action="store_true", dest="raw_style",
                    help="keep Keynote's default text-box style instead of copying the body style")

    sp = sub.add_parser("add-image", help="insert an image file onto a slide")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)
    sp.add_argument("--image", type=Path, required=True)
    sp.add_argument("--x", type=int, default=200)
    sp.add_argument("--y", type=int, default=200)
    sp.add_argument("--width", type=int, help="scale to this width (optional)")

    sp = sub.add_parser("add-slide", help="append a new slide")
    deck_arg(sp)
    sp.add_argument("--layout", default="Title & Bullets",
                    help="slide layout name (default: Title & Bullets)")

    sp = sub.add_parser("move-slide", help="reorder a slide")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True, help="slide to move")
    sp.add_argument("--after", type=int, required=True,
                    help="move it to just after this slide number")

    sp = sub.add_parser("layouts", help="list the theme's slide layouts")
    deck_arg(sp)

    sp = sub.add_parser("delete-item", help="delete a text item from a slide")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)
    sp.add_argument("--item", type=int, required=True)

    sp = sub.add_parser("delete-image", help="delete an image from a slide")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)
    sp.add_argument("--image", type=int, required=True, help="image index on the slide")

    sp = sub.add_parser("delete-slide", help="delete a slide")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)

    sp = sub.add_parser("set-size", help="set the font size of a text item "
                        "(safe on items with inline equations; use after keynote_iwa.py edits)")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)
    sp.add_argument("--item", type=int, required=True)
    sp.add_argument("--size", type=float, required=True)

    sp = sub.add_parser("set-geometry", help="move/resize a text item or image")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--item", type=int, help="text item index")
    g.add_argument("--image", type=int, help="image index")
    sp.add_argument("--x", type=float)
    sp.add_argument("--y", type=float)
    sp.add_argument("--width", type=float)
    sp.add_argument("--height", type=float)

    sp = sub.add_parser("verify", help="check slides are readable and render non-blank "
                        "(run after reopening a deck edited by keynote_iwa.py)")
    deck_arg(sp)
    sp.add_argument("--slides", help="range like 45-52,61 (default: all)")

    sp = sub.add_parser("rebuild-slide", help="replace slide N by a fresh 'Title & Bullets' slide "
                        "(title, body text/token, notes copied from the old slide, optional credit) "
                        "at the same position; use keynote_iwa.py replace-text to fill in math")
    deck_arg(sp)
    sp.add_argument("--slide", type=int, required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", required=True, help="plain text or a token like @@S51@@")
    sp.add_argument("--notes", help="presenter notes (default: copied from the old slide)")
    sp.add_argument("--credit", help="small text box bottom-right, e.g. 'credit: Fei Fang'")
    sp.add_argument("--layout", default="Title & Bullets")
    sp.add_argument("--keep-old", action="store_true", help="leave the old slide in place after the new one")

    sp = sub.add_parser("backups", help="list backups of this deck (both tools write here)")
    deck_arg(sp)

    sp = sub.add_parser("backup", help="force a backup copy of the deck now")
    deck_arg(sp)

    sp = sub.add_parser("save", help="save the deck to disk")
    deck_arg(sp)

    sp = sub.add_parser("close", help="close the deck (saving first)")
    deck_arg(sp)

    a = p.parse_args()

    try:
        if a.cmd == "list":
            for n in open_documents():
                print(n)
            return 0

        if a.cmd == "open":
            print(ensure_open(a.deck))
            return 0

        if a.cmd == "outline":
            doc = ensure_open(a.deck)
            data = extract(doc, geometry=a.geometry)
            want = parse_range(a.slides, len(data["slides"]))
            if a.json:
                data["slides"] = [s for s in data["slides"] if s["n"] in want]
                print(json.dumps(data, indent=1, ensure_ascii=False))
            else:
                print(to_markdown(data, want, a.notes))
            return 0

        if a.cmd == "render":
            paths = render(a.deck, a.slides, a.width, a.out, a.refresh)
            for pth in paths:
                print(pth)
            return 0

        doc = ensure_open(a.deck)
        if a.cmd in MUTATING:
            b = rolling_backup(a.deck)
            if b:
                print(f"backup: {b}", file=sys.stderr)

        if a.cmd == "set-text":
            # Setting object text via AppleScript silently destroys every
            # inline equation (￼ anchor) in the item. Refuse unless forced;
            # math-bearing text must be edited with keynote_iwa.py instead.
            cur = applescript(
                f'return object text of text item {a.item} of slide {a.slide} '
                f'of document "{esc(doc)}"'
            )
            if "\ufffc" in cur and not a.force:
                raise KeynoteError(
                    f"slide {a.slide} item {a.item} contains {cur.count(chr(0xFFFC))} "
                    "inline equation(s); set-text would delete them. Use "
                    "keynote_iwa.py replace-text / insert-text for this item, "
                    "or pass --force to discard the equations."
                )
            applescript(
                f'set object text of text item {a.item} of slide {a.slide} '
                f'of document "{esc(doc)}" to "{esc(a.text)}"'
            )
        elif a.cmd == "set-notes":
            applescript(
                f'set presenter notes of slide {a.slide} of document '
                f'"{esc(doc)}" to "{esc(a.text)}"'
            )
        elif a.cmd == "add-text":
            # `make new` only works inside a `tell slide` block on Keynote 15.
            applescript(
                f'tell slide {a.slide} of document "{esc(doc)}" to '
                f'make new text item with properties {{object text:"{esc(a.text)}", '
                f'position:{{{a.x}, {a.y}}}, width:{a.width}}}'
            )
            # New text boxes come out in Keynote's default style (small, bold,
            # centered). Copy font/size/color from the deck's first body
            # placeholder so they match the deck; alignment is not scriptable.
            if not a.raw_style:
                try:
                    applescript(f'''tell document "{esc(doc)}"
  set ref to missing value
  repeat with i from 1 to count of slides
    if (name of base layout of slide i) contains "Bullets" and (count of text items of slide i) >= 2 then
      set ref to text item 2 of slide i
      exit repeat
    end if
  end repeat
  if ref is not missing value then
    set n to count of text items of slide {a.slide}
    set ti to text item n of slide {a.slide}
    set font of object text of ti to (font of object text of ref)
    set size of object text of ti to (size of object text of ref)
    set color of object text of ti to (color of object text of ref)
  end if
end tell''')
                except KeynoteError as exc:
                    print(f"warning: could not copy body style: {exc}", file=sys.stderr)
        elif a.cmd == "add-image":
            img = a.image.expanduser().resolve()
            if not img.exists():
                raise KeynoteError(f"no such image: {img}")
            props = [f'file:POSIX file "{esc(str(img))}"',
                     f"position:{{{a.x}, {a.y}}}"]
            if a.width:
                props.append(f"width:{a.width}")
            applescript(
                f'tell slide {a.slide} of document "{esc(doc)}" to '
                f'make new image with properties {{{", ".join(props)}}}'
            )
        elif a.cmd == "add-slide":
            applescript(
                f'tell document "{esc(doc)}" to make new slide with properties '
                f'{{base layout:slide layout "{esc(a.layout)}"}}'
            )
        elif a.cmd == "move-slide":
            applescript(
                f'tell document "{esc(doc)}" to '
                f'move slide {a.slide} to after slide {a.after}'
            )
        elif a.cmd == "layouts":
            print(applescript(
                f'return name of every slide layout of document "{esc(doc)}"'))
            return 0
        elif a.cmd == "delete-item":
            applescript(
                f'tell slide {a.slide} of document "{esc(doc)}" to '
                f'delete text item {a.item}'
            )
        elif a.cmd == "delete-image":
            applescript(
                f'tell slide {a.slide} of document "{esc(doc)}" to '
                f'delete image {a.image}'
            )
        elif a.cmd == "delete-slide":
            applescript(f'tell document "{esc(doc)}" to delete slide {a.slide}')
        elif a.cmd == "set-size":
            applescript(
                f'tell slide {a.slide} of document "{esc(doc)}" to '
                f'set size of object text of text item {a.item} to {a.size:g}'
            )
        elif a.cmd == "set-geometry":
            ref = f'text item {a.item}' if a.item is not None else f'image {a.image}'
            lines = []
            if a.x is not None or a.y is not None:
                if a.x is None or a.y is None:
                    raise KeynoteError("--x and --y must be given together")
                lines.append(f'set position of {ref} to {{{a.x:g}, {a.y:g}}}')
            if a.width is not None:
                lines.append(f'set width of {ref} to {a.width:g}')
            if a.height is not None:
                lines.append(f'set height of {ref} to {a.height:g}')
            if not lines:
                raise KeynoteError("nothing to set")
            body = "\n".join(lines)
            applescript(f'tell slide {a.slide} of document "{esc(doc)}"\n{body}\nend tell')
        elif a.cmd == "verify":
            total = int(applescript(f'count of slides of document "{esc(doc)}"').strip())
            want = parse_range(a.slides, total)
            bad = []
            for n in want:
                try:
                    cnt = int(applescript(f'tell slide {n} of document "{esc(doc)}" to '
                                          f'count of text items').strip())
                    applescript(f'tell slide {n} of document "{esc(doc)}" to get presenter notes as string')
                    for k in range(1, cnt + 1):
                        applescript(f'tell slide {n} of document "{esc(doc)}" to get '
                                    f'(object text of text item {k}) as string')
                except KeynoteError as exc:
                    bad.append((n, f"AppleScript cannot read it: {str(exc).splitlines()[-1][:80]}"))
            paths = render(a.deck, a.slides, 600, None, refresh=True)
            for n, pth in zip(want, paths):
                if pth.stat().st_size < 2_500:   # a 600-px black JPEG is ~1.6 KB; sparse real slides are 4 KB+
                    bad.append((n, f"render is {pth.stat().st_size} bytes (blank/black?)"))
            if bad:
                for n, why in bad:
                    print(f"slide {n}: {why}")
                raise KeynoteError(f"{len(bad)} problem(s) in {len(want)} slide(s)")
            print(f"ok: {len(want)} slide(s) readable and rendered")
            return 0
        elif a.cmd == "rebuild-slide":
            old = a.slide
            notes = a.notes
            if notes is None:
                notes = applescript(f'tell document "{esc(doc)}" to get presenter notes of slide {old} as string')
            applescript(f'tell document "{esc(doc)}" to make new slide with properties '
                        f'{{base layout:slide layout "{esc(a.layout)}"}}')
            n = int(applescript(f'count of slides of document "{esc(doc)}"').strip())
            applescript(f'tell slide {n} of document "{esc(doc)}" to set object text of text item 1 to "{esc(a.title)}"')
            applescript(f'tell slide {n} of document "{esc(doc)}" to set object text of text item 2 to "{esc(a.body)}"')
            applescript(f'tell slide {n} of document "{esc(doc)}" to set presenter notes to "{esc(notes)}"')
            if a.credit:
                applescript(f'tell slide {n} of document "{esc(doc)}" to make new text item with properties '
                            f'{{object text:"{esc(a.credit)}", position:{{1560, 1000}}, width:320}}')
                applescript(f'tell slide {n} of document "{esc(doc)}" to set size of object text of text item 3 to 24')
            applescript(f'tell document "{esc(doc)}" to move slide {n} to after slide {old}')
            if not a.keep_old:
                applescript(f'tell document "{esc(doc)}" to delete slide {old}')
            print(f"rebuilt slide {old} (new slide at {old if not a.keep_old else old + 1}); "
                  f"now: keynote.py close, keynote_iwa.py replace-text --slide {old} --no-bullets --find '{a.body}' ...")
            return 0
        elif a.cmd == "backups":
            deck = a.deck.expanduser().resolve()
            for p in sorted(BACKUP_DIR.glob(f"{deck.stem}-*{deck.suffix}")):
                print(f"{p.stat().st_size/1e6:8.1f} MB  {p}")
            return 0
        elif a.cmd == "backup":
            print(rolling_backup(a.deck, min_age_s=0))
            return 0
        elif a.cmd in ("save", "close"):
            # `close ... saving yes` can silently fail to write (seen repeatedly on a
            # OneDrive-hosted deck); save explicitly and verify the document is
            # no longer dirty before trusting it.
            was_dirty = applescript(f'get modified of document "{esc(doc)}"').strip() == "true"
            before = a.deck.stat().st_mtime_ns if a.deck.exists() else 0
            applescript(f'save document "{esc(doc)}"')
            # Keynote's save returns before the bytes are on disk; `modified`
            # flips to false immediately and a close right after cancels the
            # write (this lost edits twice). Wait for the file to change.
            if was_dirty:
                import time
                deadline = time.time() + 90
                while time.time() < deadline and a.deck.stat().st_mtime_ns == before:
                    time.sleep(0.5)
                if a.deck.stat().st_mtime_ns == before:
                    raise KeynoteError(
                        "Keynote reported the save but the file on disk did not change "
                        "within 90 s. Save by hand in Keynote (File > Save), check for a "
                        "'modified by another application' dialog, then retry.")
                time.sleep(1.0)   # let the file provider settle
            if applescript(f'get modified of document "{esc(doc)}"').strip() == "true":
                raise KeynoteError("document still marked modified after save")
            if a.cmd == "close":
                applescript(f'close document "{esc(doc)}" saving no')
            print(f"saved ({'wrote changes' if was_dirty else 'no pending changes'})")
            return 0
        print("ok")
        return 0

    except KeynoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
