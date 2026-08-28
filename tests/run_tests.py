#!/usr/bin/env python3
"""End-to-end tests for the keynote skill. Needs Keynote (macOS) and uv.

    python3 ~/.claude/skills/keynote/tests/run_tests.py

Copies tests/fixture.key to a temp dir, exercises every mutator of keynote.py
(AppleScript) and keynote_iwa.py (package), then reopens the deck and checks
text, notes, equations, colors and sizes survived. Prints PASS/FAIL per check
and exits non-zero on any failure. The fixture is never modified.
"""
from __future__ import annotations
import base64, os, shutil, subprocess, sys, tempfile, time, zlib, struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
K = [sys.executable, str(HERE.parent / "scripts/keynote.py")]
I = ["uv", "run", "--with", "keynote-parser", "--with", "pyyaml", "python", str(HERE.parent / "scripts/keynote_iwa.py")]
FAILS: list[str] = []


def run(cmd, ok=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if ok and r.returncode:
        raise RuntimeError(f"{' '.join(map(str, cmd[-6:]))}\n{r.stdout}\n{r.stderr}")
    return r


def osa(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout.strip()


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def png_1x1(path: Path):
    raw = b"\x00\xff\x00\x00"  # one red pixel, filter byte 0
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="keynote-skill-test-"))
    deck = tmp / "t.key"
    shutil.copy2(HERE / "fixture.key", deck)
    img = tmp / "dot.png"; png_1x1(img)
    doc = deck.name
    print(f"deck: {deck}")

    # ---------- AppleScript layer ----------
    out = run(K + ["outline", str(deck)]).stdout
    check("outline lists 7 slides", "7 slides" in out, out[:60])
    run(K + ["set-text", str(deck), "--slide", "2", "--item", "1", "--text", "Plain bullets (edited)"])
    run(K + ["set-notes", str(deck), "--slide", "2", "--text", "Edited notes."])
    run(K + ["add-text", str(deck), "--slide", "2", "--text", "credit: tests", "--x", "1500", "--y", "1000", "--width", "300"])
    run(K + ["add-image", str(deck), "--slide", "2", "--image", str(img), "--x", "100", "--y", "900", "--width", "50"])
    run(K + ["set-geometry", str(deck), "--slide", "2", "--image", "1", "--x", "200", "--y", "900", "--width", "80"])
    run(K + ["set-size", str(deck), "--slide", "2", "--item", "3", "--size", "20"])
    run(K + ["add-slide", str(deck)])                      # 8 slides
    run(K + ["move-slide", str(deck), "--slide", "8", "--after", "1"])   # new blank slide becomes 2
    run(K + ["delete-slide", str(deck), "--slide", "2"])   # back to 7
    run(K + ["delete-image", str(deck), "--slide", "2", "--image", "1"])
    run(K + ["rebuild-slide", str(deck), "--slide", "5", "--title", "Rebuilt", "--body", "@@RB@@", "--credit", "credit: rb"])
    r = run(K + ["close", str(deck)])
    check("verified close wrote changes", "wrote changes" in r.stdout, r.stdout)
    st = run(K + ["outline", str(deck), "--slides", "2,5", "--notes"]).stdout
    run(K + ["close", str(deck)])
    check("set-text persisted", "Plain bullets (edited)" in st)
    check("set-notes persisted", "Edited notes." in st)
    check("add-text persisted", "credit: tests" in st)
    check("rebuild-slide persisted (title, token, credit)", all(x in st for x in ("Rebuilt", "@@RB@@", "credit: rb")))
    check("old slide 5 notes were not required; slide count still 7", "7 slides" in st)

    # ---------- package layer (deck closed) ----------
    r = run(I + ["insert-text", str(deck), "--slide", "3", "--after", "Before the equation.",
                 "--text", r" Here: $\frac{a}{b} = \sqrt{x^2+1}$."])
    check("insert-text created an equation in an equation-free deck", "1 equation" in r.stdout, r.stdout + r.stderr)
    r = run(I + ["replace-text", str(deck), "--slide", "5", "--no-bullets", "--find", "@@RB@@",
                 "--replace", "Line one $\\alpha$\nLine two $\\beta \\geq 0$\nLine three"])
    check("replace-text on token (multi-paragraph, 2 equations)", "2 new equation" in r.stdout, r.stdout + r.stderr)
    r = run(I + ["replace-text", str(deck), "--slide", "4", "--find", "blue words", "--replace", "BLUE words"])
    check("replace-text inside a colored run", "replaced" in r.stdout, r.stdout + r.stderr)
    eqs = run(I + ["equations", str(deck), "--slides", "5"]).stdout
    check("equations lists the new equations", "\\alpha" in eqs and "\\beta" in eqs, eqs)
    r = run(I + ["set-equation", str(deck), "--slide", "5", "--eq", "2", "--latex", r"\beta > 0"])
    check("set-equation", "->" in r.stdout, r.stdout + r.stderr)
    r = run(I + ["restyle", str(deck), "--slide", "5", "--no-bullets", "--find", "Line one"])
    check("restyle", "restyled" in r.stdout, r.stdout + r.stderr)
    r = run(I + ["insert-text", str(deck), "--slide", "1", "--after", "Fixture deck", "--text", " (open guard test)"], ok=False)
    # (deck is closed, so this one succeeds; the guard is tested below)
    outl = run(I + ["outline", str(deck), "--slides", "3,4,5"]).stdout
    check("package outline shows LaTeX inline", "$\\frac{a}{b}" in outl and "$\\beta > 0$" in outl, outl)
    check("colored-run edit text", "BLUE words" in outl)

    # ---------- reopen and verify ----------
    run(K + ["open", str(deck)])
    r = run(I + ["insert-text", str(deck), "--slide", "1", "--after", "Fixture deck", "--text", " X"], ok=False)
    check("package edit refused while deck is open", r.returncode != 0 and "open in Keynote" in (r.stdout + r.stderr))
    r = run(K + ["verify", str(deck), "--slides", "1-7"], ok=False)
    check("verify: all slides readable and non-blank", r.returncode == 0, r.stdout + r.stderr)
    body5 = osa(f'tell application id "com.apple.Keynote" to tell slide 5 of document "{doc}" to get (object text of text item 2) as string')
    check("slide 5 text has 3 paragraphs and 2 anchors", body5.count("\r") + body5.count("\n") == 2 and body5.count("￼") == 2, repr(body5))
    size5 = osa(f'tell application id "com.apple.Keynote" to tell slide 5 of document "{doc}" to get size of character 1 of object text of text item 2')
    check("slide 5 body kept the body font size (not 12 pt)", size5 not in ("12.0", "12"), size5)
    font5 = osa(f'tell application id "com.apple.Keynote" to tell slide 5 of document "{doc}" to get font of character 1 of object text of text item 2')
    check("slide 5 body font is the deck body font", "Helvetica" in font5, font5)
    col = osa(f'tell application id "com.apple.Keynote" to tell slide 4 of document "{doc}" to get color of character 1 of object text of text item 2')
    r_, g_, b_ = [int(x) for x in col.split(",")[:3]]
    check("slide 4 red run survived package edit", r_ > 40000 and g_ < 10000, col)
    col2 = osa(f'tell application id "com.apple.Keynote" to tell slide 4 of document "{doc}" to get color of character 16 of object text of text item 2')
    r2, g2, b2 = [int(x) for x in col2.split(",")[:3]]
    check("slide 4 blue run survived (edited inside it)", b2 > 40000 and r2 < 10000, col2)
    addtxt = osa(f'tell application id "com.apple.Keynote" to tell slide 2 of document "{doc}" to get {{font of object text of text item 3, size of object text of text item 3}}')
    check("add-text copied the body font (then set-size 20 applied)", "Helvetica" in addtxt and "20" in addtxt, addtxt)
    run(K + ["close", str(deck)])
    r = run(K + ["backups", str(deck)]).stdout
    check("rolling backup exists for the test deck", "t-" in r and ".key" in r, r)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
