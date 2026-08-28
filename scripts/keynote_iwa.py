#!/usr/bin/env python3
"""File-level access to Keynote decks: text WITH math, and equation editing.

Keynote stores each equation as a rendered PDF plus its LaTeX source
(`TSWP.EquationInfoArchive.equation_source_text`) inside the Snappy/protobuf
IWA archives of the .key package. AppleScript cannot see or touch that, so this
tool works on the package itself via `keynote-parser` (unpack -> YAML -> pack).

Keynote re-typesets every equation from its source text when it opens a deck,
so editing math is just editing a string here — no PDF rendering needed.

Run with:  uv run --with keynote-parser --with pyyaml python keynote_iwa.py ...
See ../SKILL.md for usage guidance and known quirks.
"""

from __future__ import annotations

import argparse
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("needs pyyaml: run via  uv run --with keynote-parser --with pyyaml python keynote_iwa.py")

OBJ = "￼"  # object-replacement char: anchors an inline attachment in text
CACHE_ROOT = Path.home() / "Library" / "Caches" / "claude-keynote"
EQ_TEXT = "[TSWP.EquationInfoArchive.equation_source_text]"
EQ_OLD = "[TSWP.EquationInfoArchive.equation_source_old]"
EQ_PROPS = "[TSWP.EquationInfoArchive.equation_text_properties]"
# Attribute tables whose entries sit on paragraph starts (one per paragraph
# when dense). Everything else keyed by characterIndex is character-level.
PARA_TABLES = {"tableParaStyle", "tableListStyle", "tableParaBidi",
               "tableParaData", "tableParaStarts", "tableDropCapStyle"}


class DeckError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# unpack / pack (keynote-parser CLI), cached per deck version
# --------------------------------------------------------------------------

def _kp(*args: str) -> None:
    cmd = [sys.executable, "-m", "keynote_parser.command_line", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-8:]
        raise DeckError("keynote-parser failed:\n" + "\n".join(tail))


def deck_key(deck: Path) -> str:
    """Cache key = hash of the file's bytes, so a cached unpack can never be
    stale relative to what is on disk (mtime/size were not reliable enough:
    Keynote and OneDrive rewrite the file in ways that kept them the same)."""
    h = hashlib.sha1()
    with open(deck, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def unpack(deck: Path, force: bool = False) -> Path:
    """Unpack `deck` into the cache (or reuse); returns the unpack dir."""
    deck = deck.expanduser().resolve()
    if not deck.exists():
        raise DeckError(f"no such deck: {deck}")
    out = CACHE_ROOT / f"{deck.stem}-{deck_key(deck)}" / "iwa"
    if force and out.exists():
        shutil.rmtree(out)
    if not (out / "Index" / "Document.iwa.yaml").exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            shutil.rmtree(out)
        _kp("unpack", str(deck), "-o", str(out))
    return out


def pack(unpack_dir: Path, out_deck: Path) -> None:
    out_deck.parent.mkdir(parents=True, exist_ok=True)
    _kp("pack", str(unpack_dir), "-o", str(out_deck))


def working_copy(deck: Path) -> Path:
    """A throwaway copy of the cached unpack to mutate (keeps the cache clean)."""
    src = unpack(deck)
    dst = src.parent / f"work-{int(time.time() * 1000)}"
    shutil.copytree(src, dst)
    return dst


# --------------------------------------------------------------------------
# YAML model helpers
# --------------------------------------------------------------------------

def load_yaml(p: Path) -> dict:
    with open(p) as fh:
        return yaml.safe_load(fh)


def save_yaml(p: Path, d: dict) -> None:
    with open(p, "w") as fh:
        yaml.safe_dump(d, fh, allow_unicode=True, sort_keys=False, width=10**9)


def archives(doc: dict):
    """Yield (chunk, archive) for every archive in a decoded IWA file."""
    for ch in doc["chunks"]:
        for ai in ch["archives"]:
            yield ch, ai


def obj_of(ai: dict) -> dict:
    return ai["objects"][0] if ai.get("objects") else {}


def mi_of(ai: dict) -> dict:
    mis = ai["header"].get("messageInfos") or [{}]
    return mis[0]


def is_equation(o: dict) -> bool:
    return EQ_TEXT in o


def slide_order(idx: Path) -> list[str]:
    """Slide archive ids in presentation order (includes skipped slides)."""
    doc = load_yaml(idx / "Document.iwa.yaml")
    objs = {str(ai["header"]["identifier"]): obj_of(ai) for _, ai in archives(doc)}
    show = next(o for o in objs.values() if o.get("_pbtype") == "KN.ShowArchive")
    nodes = [str(s["identifier"]) for s in show["slideTree"]["slides"]]
    return [str(objs[n]["slide"]["identifier"]) for n in nodes]


_SLIDE_FILE_INDEX: dict = {}


def slide_file(idx: Path, sid: str) -> Path | None:
    p = idx / f"Slide-{sid}.iwa.yaml"
    if p.exists():
        return p
    key = str(idx)
    if key not in _SLIDE_FILE_INDEX:
        # Keynote sometimes stores a slide in a file whose name does not carry
        # its id (e.g. plain "Slide.iwa" for a freshly created slide). Index
        # every KN.SlideArchive id -> file once per unpack.
        index = {}
        for f in glob.glob(str(idx / "Slide*.iwa.yaml")):
            m = re.fullmatch(r"Slide-(\d+)\.iwa\.yaml", Path(f).name)
            if m:
                continue
            for _, ai in archives(load_yaml(Path(f))):
                if obj_of(ai).get("_pbtype") == "KN.SlideArchive":
                    index[str(ai["header"]["identifier"])] = Path(f)
        _SLIDE_FILE_INDEX[key] = index
    return _SLIDE_FILE_INDEX[key].get(str(sid))


def slide_model(path: Path) -> dict:
    """Decode one slide file into storages (with inline $latex$) and equations."""
    doc = load_yaml(path)
    byid = {str(ai["header"]["identifier"]): ai for _, ai in archives(doc)}
    eqs = {i: obj_of(a) for i, a in byid.items() if is_equation(obj_of(a))}
    att = {i: str(obj_of(a)["drawable"]["identifier"]) for i, a in byid.items()
           if obj_of(a).get("_pbtype") == "TSWP.DrawableAttachmentArchive"}

    storages, used, eq_list = [], set(), []
    for i, a in byid.items():
        o = obj_of(a)
        if o.get("_pbtype") != "TSWP.StorageArchive":
            continue
        raw = "".join(o.get("text") or [])
        entries = ((o.get("tableAttachment") or {}).get("entries") or [])
        at = {}
        for e in entries:
            did = att.get(str(e["object"]["identifier"]))
            if did in eqs:
                at[e["characterIndex"]] = did
                used.add(did)
        out = []
        for ci, chr_ in enumerate(raw):
            if chr_ == OBJ and ci in at:
                did = at[ci]
                eq_list.append({"id": did, "latex": eqs[did][EQ_TEXT], "storage": i,
                                "index": ci,
                                "fontSize": (eqs[did].get(EQ_PROPS) or {}).get("fontSize")})
                out.append("$" + eqs[did][EQ_TEXT].strip() + "$")
            else:
                out.append(chr_)
        storages.append({"id": i, "kind": o.get("kind"), "text": "".join(out),
                         "raw": raw, "n_eq": len(at)})
    for did, o in eqs.items():
        if did not in used:
            eq_list.append({"id": did, "latex": o[EQ_TEXT], "storage": None,
                            "index": None, "floating": True,
                            "fontSize": (o.get(EQ_PROPS) or {}).get("fontSize")})
    # Stable numbering for --eq K: by storage id then position, floating last.
    eq_list.sort(key=lambda e: (e["storage"] is None, e["storage"] or "", e["index"] or 0))
    for k, e in enumerate(eq_list, 1):
        e["k"] = k
    return {"storages": storages, "equations": eq_list}


def deck_model(deck: Path) -> dict:
    """Whole-deck model, cached as JSON next to the unpack."""
    udir = unpack(deck)
    cache = udir.parent / "model.json"
    if cache.exists():
        return json.loads(cache.read_text())
    idx = udir / "Index"
    order = slide_order(idx)
    slides = []
    for n, sid in enumerate(order, 1):
        p = slide_file(idx, sid)
        m = slide_model(p) if p else {"storages": [], "equations": []}
        m.update({"n": n, "sid": sid, "file": p.name if p else None})
        slides.append(m)
    model = {"deck": str(deck), "slides": slides}
    cache.write_text(json.dumps(model, ensure_ascii=False))
    return model


def parse_range(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(1, total + 1))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return [n for n in out if 1 <= n <= total]


def print_outline(model: dict, want: list[int], notes: bool) -> None:
    print(f"# {Path(model['deck']).name} — {len(model['slides'])} slides\n")
    for s in model["slides"]:
        if s["n"] not in want:
            continue
        print(f"## Slide {s['n']}")
        for st in s["storages"]:
            t = st["text"].strip()
            if not t or t == OBJ:
                continue
            if st["kind"]:          # presenter notes storage
                if notes:
                    print("  NOTES: " + t.replace("\n", "\n  "))
                continue
            print("- " + t.replace("\n", "\n  "))
        for e in s["equations"]:
            if e.get("floating"):
                print(f"- (floating eq) ${e['latex']}$")
        print()


def print_equations(model: dict, want: list[int]) -> None:
    for s in model["slides"]:
        if s["n"] not in want or not s["equations"]:
            continue
        print(f"## Slide {s['n']}")
        for e in s["equations"]:
            tag = " (floating)" if e.get("floating") else ""
            print(f"  [{e['k']}] {e['latex']}{tag}")


# --------------------------------------------------------------------------
# mutation primitives
# --------------------------------------------------------------------------

class IdAllocator:
    """Mint fresh object ids from TSP.PackageMetadata.lastObjectIdentifier."""

    def __init__(self, idx: Path):
        self.path = idx / "Metadata.iwa.yaml"
        self.doc = load_yaml(self.path)
        self.pm = next(obj_of(ai) for _, ai in archives(self.doc)
                       if obj_of(ai).get("_pbtype") == "TSP.PackageMetadata")
        self.last = int(self.pm["lastObjectIdentifier"])

    def new(self) -> str:
        self.last += 1
        return str(self.last)

    def save(self) -> None:
        self.pm["lastObjectIdentifier"] = str(self.last)
        save_yaml(self.path, self.doc)

    def component(self, comp_id: str) -> dict:
        return next(c for c in self.pm["components"] if str(c["identifier"]) == str(comp_id))

    def add_external(self, target_comp: str, source_comp: str, obj_ids) -> list[str]:
        """Make objects `obj_ids` (reachable from component `source_comp`)
        resolvable from `target_comp` by copying the matching externalReferences
        entries. Objects defined inside the source component itself get a
        reference to that component. Returns the entries that were added."""
        obj_ids = {str(o) for o in obj_ids}
        if not obj_ids or str(target_comp) == str(source_comp):
            return []
        tgt = self.component(target_comp)
        src = self.component(source_comp)
        have = {(str(e.get("componentIdentifier")), str(e.get("objectIdentifier")))
                for e in (tgt.get("externalReferences") or [])}
        src_refs = {str(e.get("objectIdentifier")): e
                    for e in (src.get("externalReferences") or []) if "objectIdentifier" in e}
        added = []
        tgt.setdefault("externalReferences", [])
        for oid in sorted(obj_ids, key=int):
            e = src_refs.get(oid) or {"componentIdentifier": str(source_comp), "objectIdentifier": oid}
            key = (str(e["componentIdentifier"]), str(e["objectIdentifier"]))
            if key in have:
                continue
            tgt["externalReferences"].append(dict(e))
            have.add(key)
            added.append(oid)
        return added


def _component_id_of_file(idx: Path, p: Path) -> str:
    stem = p.name[: -len(".iwa.yaml")]
    meta = load_yaml(idx / "Metadata.iwa.yaml")
    pm = next(obj_of(ai) for _, ai in archives(meta) if obj_of(ai).get("_pbtype") == "TSP.PackageMetadata")
    for c in pm["components"]:
        if str(c.get("locator") or c.get("preferredLocator") or "") == stem:
            return str(c["identifier"])
    return stem.split("-")[1] if "-" in stem else stem


def find_equation_template(idx: Path, slide_doc: dict, ids=None):
    """An existing inline equation (image archive, its attachment, captions) to
    clone. Prefer one in the same slide; fall back to any slide in the deck."""
    def search(doc):
        for ch, ai in archives(doc):
            o = obj_of(ai)
            if is_equation(o) and o.get("_pbtype") == "TSD.ImageArchive":
                byid = {str(a["header"]["identifier"]): a for _, a in archives(doc)}
                iid = str(ai["header"]["identifier"])
                att = next((a for _, a in archives(doc)
                            if obj_of(a).get("_pbtype") == "TSWP.DrawableAttachmentArchive"
                            and str(obj_of(a)["drawable"]["identifier"]) == iid), None)
                if not att:
                    continue
                caps = [str(o["super"]["caption"]["identifier"]),
                        str(o["super"]["title"]["identifier"])]
                if all(c in byid for c in caps):
                    return ai, att, [byid[c] for c in caps]
        return None
    hit = search(slide_doc)
    if hit:
        return (*hit, None)
    for p in sorted(glob.glob(str(idx / "Slide-*.iwa.yaml"))):
        hit = search(load_yaml(Path(p)))
        if hit:
            return (*hit, _component_id_of_file(idx, Path(p)))
    return synthesize_equation_template(idx, ids)


EQ_STYLE_ID = "equation-0-imageStyle"


def synthesize_equation_template(idx: Path, ids=None):
    """Deck has no inline equation to clone: build one from the minimal object
    set Keynote uses (verified against Keynote 15.1.1 decks). The media style
    lives in DocumentStylesheet and is created there if missing."""
    ss_path = idx / "DocumentStylesheet.iwa.yaml"
    ss = load_yaml(ss_path)
    own_ids = ids is None
    ids = ids or IdAllocator(idx)
    sheet_id = style_id = None
    for _, ai in archives(ss):
        o = obj_of(ai)
        if o.get("_pbtype") == "TSS.StylesheetArchive" and sheet_id is None:
            sheet_id = str(ai["header"]["identifier"]); sheet_obj = o
        if o.get("_pbtype") == "TSD.MediaStyleArchive" and (o.get("super") or {}).get("styleIdentifier") == EQ_STYLE_ID:
            style_id = str(ai["header"]["identifier"])
    if sheet_id is None:
        raise DeckError("no TSS.StylesheetArchive in DocumentStylesheet")
    if style_id is None:
        style_id = ids.new()
        black = {"a": 1.0, "r": 0.0, "g": 0.0, "b": 0.0, "model": "rgb", "rgbspace": "srgb"}
        style = {"header": {"_pbtype": "TSP.ArchiveInfo", "identifier": style_id,
                            "messageInfos": [{"type": 3016, "version": [1, 0, 5]}]},
                 "objects": [{"_pbtype": "TSD.MediaStyleArchive",
                              "mediaProperties": {"opacity": 1.0, "reflection": {}, "shadow": {},
                                                  "stroke": {"cap": "ButtCap", "color": black, "join": "MiterJoin",
                                                             "miterLimit": 4.0,
                                                             "pattern": {"count": 0, "pattern": [0.0] * 6, "phase": 0.0,
                                                                         "type": "TSDEmptyPattern"},
                                                             "width": 1.0}},
                              "overrideCount": 4,
                              "super": {"styleIdentifier": EQ_STYLE_ID, "stylesheet": {"identifier": sheet_id}}}]}
        ss["chunks"][0]["archives"].append(style)
        # register in the stylesheet's identifier -> style map if it has one
        for k, v in list(sheet_obj.items()):
            if isinstance(v, list) and v and isinstance(v[0], dict) and set(v[0]) >= {"identifier", "style"} \
                    and any(isinstance(e.get("identifier"), str) for e in v):
                v.append({"identifier": EQ_STYLE_ID, "style": {"identifier": style_id}})
                break
        save_yaml(ss_path, ss)
        if own_ids:
            ids.save()
        print(f"created equation media style {style_id} in DocumentStylesheet", file=sys.stderr)
    dark = {"a": 1.0, "r": 0.06666667, "g": 0.06666667, "b": 0.06666667, "model": "rgb", "rgbspace": "srgb"}
    img = {"header": {"_pbtype": "TSP.ArchiveInfo", "identifier": "0",
                      "messageInfos": [{"type": 3005, "version": [1, 0, 5], "objectReferences": ["1", "2", style_id]}]},
           "objects": [{"_pbtype": "TSD.ImageArchive",
                        EQ_TEXT: "x", EQ_OLD: "x",
                        "[TSWP.EquationInfoArchive.equation_depth]": 0.0,
                        EQ_PROPS: {"fontColor": dark, "fontName": "HelveticaNeue", "fontSize": 48.0,
                                   "tsdFill": {"color": dark}},
                        "flags": 0, "interpretsUntaggedImageDataAsGeneric": False,
                        "naturalSize": {"height": 0.0, "width": 0.0},
                        "originalSize": {"height": 48.0, "width": 48.0},
                        "style": {"identifier": style_id},
                        "super": {"aspectRatioLocked": True, "caption": {"identifier": "1"}, "captionHidden": False,
                                  "exteriorTextWrap": {"alphaThreshold": 0.5, "direction": 2, "fitType": 1,
                                                       "isHtmlWrap": False, "margin": 12.0, "type": 0},
                                  "geometry": {"angle": 0.0, "flags": 3, "position": {"x": 0.0, "y": 0.0},
                                               "size": {"height": 48.0, "width": 48.0}},
                                  "locked": False, "parent": {"identifier": "0"},
                                  "title": {"identifier": "2"}, "titleHidden": False}}]}
    att = {"header": {"_pbtype": "TSP.ArchiveInfo", "identifier": "0",
                      "messageInfos": [{"type": 2003, "version": [1, 0, 5], "objectReferences": ["0"]}]},
           "objects": [{"_pbtype": "TSWP.DrawableAttachmentArchive", "drawable": {"identifier": "0"},
                        "hOffset": 0.0, "hOffsetType": 0, "vOffset": 0.0, "vOffsetType": 0}]}
    cap = lambda i: {"header": {"_pbtype": "TSP.ArchiveInfo", "identifier": i,
                                "messageInfos": [{"type": 3097, "version": [10, 1, 0]}]},
                     "objects": [{"_pbtype": "TSD.StandinCaptionArchive"}]}
    ss_comp = _component_id_of_file(idx, ss_path)
    return img, att, [cap("1"), cap("2")], ss_comp


def split_math(text: str):
    """'a $x^2$ b' -> ('a ￼ b', [(offset, 'x^2')]).  $$ escapes a literal $."""
    out, eqs = [], []
    pos = 0
    for m in re.finditer(r"\$\$|\$([^$]+)\$", text):
        out.append(text[pos:m.start()])
        if m.group(0) == "$$":
            out.append("$")
        else:
            cur = sum(len(x) for x in out)
            eqs.append((cur, m.group(1).strip()))
            out.append(OBJ)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), eqs


def para_starts(text: str) -> list[int]:
    starts = [0]
    for i, c in enumerate(text):
        if c == "\n" and i + 1 < len(text):
            starts.append(i + 1)
    return starts


_LIST_NONE_CACHE: dict = {}


def find_list_style_none(idx: Path) -> str:
    """Id of the stylesheet's list style named 'None' (no bullet)."""
    key = str(idx)
    if key in _LIST_NONE_CACHE:
        return _LIST_NONE_CACHE[key]
    ss = load_yaml(idx / "DocumentStylesheet.iwa.yaml")
    for _, ai in archives(ss):
        o = obj_of(ai)
        if o.get("_pbtype") == "TSWP.ListStyleArchive" and (o.get("super") or {}).get("name") == "None":
            _LIST_NONE_CACHE[key] = str(ai["header"]["identifier"])
            return _LIST_NONE_CACHE[key]
    raise DeckError("no list style named 'None' in DocumentStylesheet")


def component_id_for(doc: dict, ids) -> str:
    """Metadata component id of the file `doc` was loaded from (its locator is
    the file stem, e.g. 'Slide-123' or 'Slide'), falling back to the slide id."""
    loc = doc.get("_locator")
    if loc:
        for c in ids.pm["components"]:
            if str(c.get("locator") or c.get("preferredLocator") or "") == loc:
                return str(c["identifier"])
    return next(str(o["header"]["identifier"]) for _, o in archives(doc)
                if obj_of(o).get("_pbtype") == "KN.SlideArchive")


def _declare_stylesheet_ref(doc: dict, ids, obj_id: str) -> None:
    """Make a DocumentStylesheet object resolvable from this slide's component."""
    this_comp = component_id_for(doc, ids)
    tgt = ids.component(this_comp)
    have = {(str(e.get("componentIdentifier")), str(e.get("objectIdentifier")))
            for e in (tgt.get("externalReferences") or [])}
    ss_comp = next(str(c["identifier"]) for c in ids.pm["components"]
                   if "DocumentStylesheet" in str(c.get("locator") or c.get("preferredLocator") or ""))
    if (ss_comp, str(obj_id)) not in have:
        tgt.setdefault("externalReferences", []).append(
            {"componentIdentifier": ss_comp, "objectIdentifier": str(obj_id)})


def edit_storage(doc: dict, st_ai: dict, pos: int, del_len: int, ins: str,
                 eqs, idx: Path, ids: IdAllocator, chunk: dict,
                 eq_size: float | None = None, no_bullets: bool = False) -> list[str]:
    """Replace text[pos:pos+del_len] with `ins` (which may contain OBJ anchors
    for `eqs`) and keep every attribute table consistent. Returns new eq ids."""
    st = obj_of(st_ai)
    mi = mi_of(st_ai)
    sid = str(st_ai["header"]["identifier"])
    old = "".join(st.get("text") or [])
    end = pos + del_len
    new = old[:pos] + ins + old[end:]
    delta = len(ins) - del_len

    def remap(i: int) -> int:
        if i < pos:
            return i
        if i < end:
            return pos
        return i + delta

    old_paras = para_starts(old)
    new_paras = para_starts(new)
    byid = {str(a["header"]["identifier"]): a for _, a in archives(doc)}
    removed_attachments: list[str] = []

    for k, v in list(st.items()):
        if not (k.startswith("table") and isinstance(v, dict) and "entries" in v):
            continue
        entries = v["entries"]
        if k == "tableAttachment":
            kept = []
            for e in entries:
                ci = e["characterIndex"]
                if pos <= ci < end:                       # anchor deleted with its text
                    removed_attachments.append(str(e["object"]["identifier"]))
                    continue
                e["characterIndex"] = remap(ci)
                kept.append(e)
            v["entries"] = kept
        elif k in PARA_TABLES and (len(new_paras) > 1 or len(entries) > 1):
            # Dense paragraph table: rebuild so there is exactly one entry per
            # new paragraph, inheriting from the paragraph the text came from.
            def governing(old_i):
                g = entries[0]
                for e in entries:
                    if e["characterIndex"] <= old_i:
                        g = e
                return g
            rebuilt = []
            for s in new_paras:
                # map new index back to an old index
                if s < pos:
                    oi = s
                elif s < pos + len(ins):
                    oi = pos if pos < len(old) else max(0, len(old) - 1)
                else:
                    oi = s - delta
                e = copy.deepcopy(governing(oi))
                e["characterIndex"] = s
                rebuilt.append(e)
            v["entries"] = rebuilt
        else:
            # Character-level (or sparse paragraph) table: shift; entries inside
            # the deleted range collapse onto `pos`, keeping the last one.
            seen = {}
            for e in entries:
                e["characterIndex"] = remap(e["characterIndex"])
                seen[e["characterIndex"]] = e
            v["entries"] = [seen[i] for i in sorted(seen)]

    st["text"] = [new]

    # Drop orphaned equation objects whose anchors were deleted.
    for aid in removed_attachments:
        a = byid.get(aid)
        if a and str(aid) in (mi.get("objectReferences") or []):
            mi["objectReferences"].remove(str(aid))
        if a:
            did = str(obj_of(a)["drawable"]["identifier"])
            for victim in (aid, did):
                for ch in doc["chunks"]:
                    ch["archives"] = [x for x in ch["archives"]
                                      if str(x["header"]["identifier"]) != victim]

    # Create new equations for the anchors in `ins`.
    new_ids = []
    if eqs:
        t_img, t_att, t_caps, t_comp = find_equation_template(idx, doc, ids)
        t_caps_ids = [str(c["header"]["identifier"]) for c in t_caps]
        if t_comp is not None:
            # The template lives in another slide's component: every object it
            # references that is not defined in this slide's file must be
            # declared in this component's externalReferences, or Keynote
            # cannot load the slide (renders black, AppleScript -10000).
            def _refs(x, acc):
                if isinstance(x, dict):
                    if set(x.keys()) == {"identifier"}:
                        acc.add(str(x["identifier"]))
                    for v in x.values():
                        _refs(v, acc)
                elif isinstance(x, list):
                    for v in x:
                        _refs(v, acc)
            needed = set()
            for a in (t_img, t_att, *t_caps):
                _refs(a["objects"], needed)
                needed.update(str(r) for r in (mi_of(a).get("objectReferences") or []))
            needed -= set(byid)
            needed -= {str(t_img["header"]["identifier"]), str(t_att["header"]["identifier"]), *t_caps_ids}
            needed -= {"0", "1", "2"}                      # synthesized-template placeholders
            needed.discard(str((obj_of(t_img).get("data") or {}).get("identifier")))   # PDF data ref
            needed.discard(str((obj_of(t_img)["super"].get("parent") or {}).get("identifier")))
            this_comp = component_id_for(doc, ids)
            added = ids.add_external(this_comp, t_comp, needed)
            if added:
                print(f"declared external refs {added} for slide component {this_comp}", file=sys.stderr)
        if st.get("tableAttachment") is None:
            st["tableAttachment"] = {"entries": []}
        mi.setdefault("objectReferences", [])
        for off, latex in eqs:
            img_id, att_id, c1, c2 = ids.new(), ids.new(), ids.new(), ids.new()
            img = copy.deepcopy(t_img)
            img["header"]["identifier"] = img_id
            imi = mi_of(img)
            imi.pop("dataReferences", None)       # no PDF: Keynote re-renders
            imi["objectReferences"] = [c1, c2] + [
                r for r in (imi.get("objectReferences") or []) if str(r) not in t_caps_ids]
            o = obj_of(img)
            o.pop("data", None)
            o[EQ_TEXT] = latex
            if EQ_OLD in o:
                o[EQ_OLD] = latex
            if eq_size is not None and isinstance(o.get(EQ_PROPS), dict):
                o[EQ_PROPS]["fontSize"] = float(eq_size)
            o["super"]["caption"] = {"identifier": c1}
            o["super"]["title"] = {"identifier": c2}
            o["super"]["parent"] = {"identifier": sid}
            for cid, tc in zip((c1, c2), t_caps):
                cc = copy.deepcopy(tc)
                cc["header"]["identifier"] = cid
                chunk["archives"].append(cc)
            att = copy.deepcopy(t_att)
            att["header"]["identifier"] = att_id
            mi_of(att)["objectReferences"] = [img_id]
            obj_of(att)["drawable"] = {"identifier": img_id}
            chunk["archives"] += [img, att]
            st["tableAttachment"]["entries"].append(
                {"characterIndex": pos + off, "object": {"identifier": att_id}})
            mi["objectReferences"].append(att_id)
            new_ids.append(img_id)
        st["tableAttachment"]["entries"].sort(key=lambda e: e["characterIndex"])

    if no_bullets:
        none_id = find_list_style_none(idx)
        st["tableListStyle"] = {"entries": [{"characterIndex": 0, "object": {"identifier": none_id}}]}
        _declare_stylesheet_ref(doc, ids, none_id)

    # Sanity: every index must lie inside the new text, sorted and unique.
    for k, v in st.items():
        if k.startswith("table") and isinstance(v, dict) and "entries" in v:
            idxs = [e["characterIndex"] for e in v["entries"]]
            if any(i > len(new) for i in idxs) or idxs != sorted(set(idxs)):
                raise DeckError(f"internal: inconsistent {k} after edit: {idxs}")
    return new_ids


def locate(model_slide: dict, anchor: str, nth: int):
    """Find which storage on the slide contains `anchor` (in plain text, with
    $latex$ rendered as in `outline`). Returns (storage_id, raw_pos, raw_len)."""
    hits = []
    for st in model_slide["storages"]:
        # Work on the $latex$ view so users can anchor on math too, then map
        # the position back to the raw text with OBJ anchors.
        view = st["text"]
        start = 0
        while True:
            j = view.find(anchor, start)
            if j < 0:
                break
            hits.append((st["id"], j))
            start = j + 1
    if not hits:
        raise DeckError(f"anchor text not found on that slide: {anchor!r}")
    if len(hits) > 1 and nth is None:
        raise DeckError(f"anchor occurs {len(hits)} times on the slide; pass --nth")
    sid, j = hits[(nth or 1) - 1]
    st = next(s for s in model_slide["storages"] if s["id"] == sid)
    # map view offset -> raw offset: walk both strings together
    raw, view = st["raw"], st["text"]
    ri = vi = 0
    def advance(until_vi):
        nonlocal ri, vi
        while vi < until_vi:
            if raw[ri] == OBJ and view[vi] == "$":
                # skip the $...$ rendering of this anchor
                close = view.index("$", vi + 1)
                vi = close + 1
                ri += 1
            else:
                vi += 1
                ri += 1
    advance(j)
    r0 = ri
    advance(j + len(anchor))
    return sid, r0, ri - r0


# --------------------------------------------------------------------------
# high-level mutations
# --------------------------------------------------------------------------

BACKUP_KEEP = 30   # newest copies kept per deck (a 100 MB deck x 30 = 3 GB max)


def backup(deck: Path) -> Path:
    bdir = CACHE_ROOT / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dst = bdir / f"{deck.stem}-{time.strftime('%Y%m%d-%H%M%S')}{deck.suffix}"
    shutil.copy2(deck, dst)
    mine = sorted(bdir.glob(f"{deck.stem}-*{deck.suffix}"), key=lambda q: q.stat().st_mtime)
    for old in mine[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return dst


def commit(deck: Path, work: Path, out: Path | None) -> Path:
    """Pack the working copy to `out` (default: in place, after a backup)."""
    if out is None:
        b = backup(deck)
        print(f"backup: {b}", file=sys.stderr)
        out = deck
    tmp = out.with_suffix(".tmp.key")
    pack(work, tmp)
    os.replace(tmp, out)
    shutil.rmtree(work, ignore_errors=True)
    return out


def _open_in_keynote(deck: Path) -> bool:
    try:
        out = subprocess.run(["osascript", "-e",
                              'tell application id "com.apple.Keynote" to get name of every document'],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return False
    return deck.name in [x.strip() for x in out.split(",")]


def mutate_slide(deck: Path, n: int, fn) -> tuple[Path, dict]:
    """Run fn(doc, chunk_of_slide, idx, ids, model_slide) on a working copy."""
    if _open_in_keynote(deck):
        raise DeckError(f"{deck.name} is open in Keynote; save and close it first "
                        "(keynote.py close DECK), otherwise Keynote will overwrite this edit")
    model = deck_model(deck)
    if not 1 <= n <= len(model["slides"]):
        raise DeckError(f"slide {n} out of range 1..{len(model['slides'])}")
    ms = model["slides"][n - 1]
    if not ms["file"]:
        raise DeckError(f"slide {n} has no archive file")
    work = working_copy(deck)
    idx = work / "Index"
    p = idx / ms["file"]
    doc = load_yaml(p)
    doc["_locator"] = p.name[: -len(".iwa.yaml")]
    ids = IdAllocator(idx)
    fn(doc, doc["chunks"][0], idx, ids, ms)
    doc.pop("_locator", None)
    save_yaml(p, doc)
    ids.save()
    return work, ms


def find_para_style_named(idx: Path, name: str) -> str:
    ss = load_yaml(idx / "DocumentStylesheet.iwa.yaml")
    for _, ai in archives(ss):
        o = obj_of(ai)
        if o.get("_pbtype") == "TSWP.ParagraphStyleArchive" and (o.get("super") or {}).get("name") == name:
            return str(ai["header"]["identifier"])
    raise DeckError(f"no paragraph style named {name!r} in DocumentStylesheet")


def cmd_restyle(deck: Path, n: int, find: str, nth: int | None, para_style: str | None,
                no_bullets: bool, out: Path | None):
    """Give the text item containing `find` one paragraph-style entry per
    paragraph (style at 0, inherit elsewhere) and optionally the 'None' list
    style. Repairs items whose text was edited by older versions of this tool
    (Keynote showed them at 12 pt Helvetica) and strips layout bullets."""
    def fn(doc, chunk, idx, ids, ms):
        sid, _, _ = locate(ms, find, nth)
        st_ai = next(ai for _, ai in archives(doc) if str(ai["header"]["identifier"]) == sid)
        st = obj_of(st_ai)
        text = "".join(st.get("text") or [])
        style = para_style or find_para_style_named(idx, "Body")
        starts = para_starts(text)
        st["tableParaStyle"] = {"entries": [{"characterIndex": 0, "object": {"identifier": str(style)}}] +
                                [{"characterIndex": k} for k in starts if k > 0]}
        _declare_stylesheet_ref(doc, ids, style)
        if no_bullets:
            none_id = find_list_style_none(idx)
            st["tableListStyle"] = {"entries": [{"characterIndex": 0, "object": {"identifier": none_id}}]}
            _declare_stylesheet_ref(doc, ids, none_id)
        print(f"slide {n}: restyled storage {sid} ({len(starts)} paragraph(s), para style {style}"
              f"{', no bullets' if no_bullets else ''})")
    work, _ = mutate_slide(deck, n, fn)
    return commit(deck, work, out)


def cmd_set_equation(deck: Path, n: int, k: int, latex: str, out: Path | None):
    def fn(doc, chunk, idx, ids, ms):
        eq = next((e for e in ms["equations"] if e["k"] == k), None)
        if not eq:
            raise DeckError(f"slide {n} has no equation [{k}] (see `equations`)")
        for _, ai in archives(doc):
            if str(ai["header"]["identifier"]) == eq["id"]:
                o = obj_of(ai)
                o[EQ_TEXT] = latex
                if EQ_OLD in o:
                    o[EQ_OLD] = latex
                print(f"slide {n} eq [{k}]: {eq['latex']}  ->  {latex}")
                return
        raise DeckError("equation object vanished?")
    work, _ = mutate_slide(deck, n, fn)
    return commit(deck, work, out)


def cmd_insert(deck: Path, n: int, after: str | None, before: str | None,
               eq_size: float | None, no_bullets: bool,
               text: str, nth: int | None, out: Path | None):
    def fn(doc, chunk, idx, ids, ms):
        anchor = after if after is not None else before
        sid, r0, rl = locate(ms, anchor, nth)
        pos = r0 + rl if after is not None else r0
        ins, eqs = split_math(text)
        st_ai = next(ai for _, ai in archives(doc) if str(ai["header"]["identifier"]) == sid)
        new = edit_storage(doc, st_ai, pos, 0, ins, eqs, idx, ids, chunk, eq_size, no_bullets)
        print(f"slide {n}: inserted {len(ins)} chars, {len(new)} equation(s) at {pos}")
    work, _ = mutate_slide(deck, n, fn)
    return commit(deck, work, out)


def cmd_replace(deck: Path, n: int, find: str, repl: str, nth: int | None,
                eq_size: float | None, no_bullets: bool,
                out: Path | None):
    def fn(doc, chunk, idx, ids, ms):
        sid, r0, rl = locate(ms, find, nth)
        ins, eqs = split_math(repl)
        st_ai = next(ai for _, ai in archives(doc) if str(ai["header"]["identifier"]) == sid)
        new = edit_storage(doc, st_ai, r0, rl, ins, eqs, idx, ids, chunk, eq_size, no_bullets)
        print(f"slide {n}: replaced {rl} chars with {len(ins)}, {len(new)} new equation(s)")
    work, _ = mutate_slide(deck, n, fn)
    return commit(deck, work, out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(prog="keynote_iwa.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def deck_arg(sp):
        sp.add_argument("deck", type=Path)

    sp = sub.add_parser("outline", help="slide text with inline $latex$")
    deck_arg(sp)
    sp.add_argument("--slides")
    sp.add_argument("--notes", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("equations", help="numbered equations per slide")
    deck_arg(sp)
    sp.add_argument("--slides")

    def mut_args(sp):
        deck_arg(sp)
        sp.add_argument("--slide", type=int, required=True)
        sp.add_argument("--out", type=Path,
                        help="write to this file instead of in place (+backup)")
        sp.add_argument("--no-bullets", action="store_true", dest="no_bullets",
                        help="give the edited text item the deck's 'None' list style (no bullets)")
        sp.add_argument("--eq-size", type=float, default=None, dest="eq_size",
                        help="font size for newly created equations (default: template's)")

    sp = sub.add_parser("set-equation", help="replace one equation's LaTeX")
    mut_args(sp)
    sp.add_argument("--eq", type=int, required=True, help="[k] from `equations`")
    sp.add_argument("--latex", required=True)

    sp = sub.add_parser("insert-text",
                        help="insert text (may contain $latex$) after/before anchor text")
    mut_args(sp)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--after")
    g.add_argument("--before")
    sp.add_argument("--text", required=True)
    sp.add_argument("--nth", type=int, help="which occurrence of the anchor")

    sp = sub.add_parser("replace-text",
                        help="replace text on a slide (replacement may contain $latex$)")
    mut_args(sp)
    sp.add_argument("--find", required=True)
    sp.add_argument("--replace", required=True)
    sp.add_argument("--nth", type=int)

    sp = sub.add_parser("restyle", help="fix paragraph/list styles of a text item "
                        "(one para-style entry per paragraph; --no-bullets)")
    mut_args(sp)
    sp.add_argument("--find", required=True, help="text that identifies the item")
    sp.add_argument("--nth", type=int)
    sp.add_argument("--para-style", dest="para_style", help="paragraph style id (default: 'Body')")

    sp = sub.add_parser("unpack", help="unpack to YAML (cached); prints the dir")
    deck_arg(sp)
    sp.add_argument("--force", action="store_true")

    sp = sub.add_parser("pack", help="pack an unpack dir into a .key")
    sp.add_argument("dir", type=Path)
    sp.add_argument("--out", type=Path, required=True)

    sp = sub.add_parser("clear-cache", help="drop cached unpacks for a deck")
    deck_arg(sp)

    a = p.parse_args()
    try:
        if a.cmd == "restyle":
            print(cmd_restyle(a.deck, a.slide, a.find, a.nth, a.para_style, a.no_bullets, a.out))
            return 0
        if a.cmd == "outline":
            m = deck_model(a.deck)
            want = parse_range(a.slides, len(m["slides"]))
            if a.json:
                m["slides"] = [s for s in m["slides"] if s["n"] in want]
                print(json.dumps(m, indent=1, ensure_ascii=False))
            else:
                print_outline(m, want, a.notes)
        elif a.cmd == "equations":
            m = deck_model(a.deck)
            print_equations(m, parse_range(a.slides, len(m["slides"])))
        elif a.cmd == "set-equation":
            print(cmd_set_equation(a.deck, a.slide, a.eq, a.latex, a.out))
        elif a.cmd == "insert-text":
            print(cmd_insert(a.deck, a.slide, a.after, a.before, a.eq_size, a.no_bullets, a.text, a.nth, a.out))
        elif a.cmd == "replace-text":
            print(cmd_replace(a.deck, a.slide, a.find, a.replace, a.nth, a.eq_size, a.no_bullets, a.out))
        elif a.cmd == "unpack":
            print(unpack(a.deck, a.force))
        elif a.cmd == "pack":
            pack(a.dir, a.out)
            print(a.out)
        elif a.cmd == "clear-cache":
            d = a.deck.expanduser().resolve()
            for c in CACHE_ROOT.glob(f"{d.stem}-*"):
                shutil.rmtree(c, ignore_errors=True)
                print("removed", c)
        return 0
    except DeckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
