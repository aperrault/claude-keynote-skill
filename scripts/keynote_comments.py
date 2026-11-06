"""Keynote comments (the sticky "Kommentar" objects) – list and add, on the .key package.

  python keynote_comments.py list DECK [--slides 200-215]
  python keynote_comments.py add  DECK SPEC.json [--out OUT.key]

SPEC.json: {"author": "Claude", "template_slide": 210 (optional),
            "comments": [{"slide": 206, "text": "…", "x": 600, "y": 380, "w": 240, "h": 110}, …]}

Slide numbers are ABSOLUTE (hidden slides count). AppleScript cannot create comments; this clones an
existing comment in the deck as template (so the deck needs at least one comment, made by hand once).
New comments get their own author (created on first use, light blue). `add` writes in place after a
backup (like keynote_iwa.py mutators) – Keynote must NOT have the deck open – or to --out.
Verify by exporting a PDF with {include comments:true}. Run via:
  uv run --with keynote-parser --with pyyaml python keynote_comments.py …
"""
import copy, json, random, sys, time, uuid, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import keynote_iwa as K


def find_template_slide(model, idx):
    for n, ms in enumerate(model['slides'], 1):
        if 'CommentInfoArchive' in (idx / ms['file']).read_text():
            return n
    return None


def cmd_list(deck, want):
    model = K.deck_model(deck); idx = K.unpack(deck) / 'Index'
    authors = {}
    ap = idx / 'AnnotationAuthorStorage.iwa.yaml'
    if ap.exists():
        for _, ai in K.archives(K.load_yaml(ap)):
            o = K.obj_of(ai)
            if o.get('_pbtype') == 'TSK.AnnotationAuthorArchive':
                authors[str(ai['header']['identifier'])] = o.get('name')
    found = 0
    for n, ms in enumerate(model['slides'], 1):
        if want and n not in want:
            continue
        p = idx / ms['file']
        raw = p.read_text()
        if 'CommentInfoArchive' not in raw:
            continue
        doc = K.load_yaml(p)
        A = {str(ai['header']['identifier']): ai for _, ai in K.archives(doc)}
        for i, ai in A.items():
            o = K.obj_of(ai)
            if o.get('_pbtype') != 'TSWP.CommentInfoArchive':
                continue
            st = K.obj_of(A[str(o['super']['ownedStorage']['identifier'])])
            cs = K.obj_of(A[str(o['commentStorage']['identifier'])])
            g = o['super']['super']['super']['geometry']
            when = time.strftime('%d.%m.%Y %H:%M', time.localtime(cs['creationDate']['seconds'] + 978307200))
            print(f"slide {n} | {authors.get(str(cs['author']['identifier']), '?')} | {when} | "
                  f"({g['position']['x']:.0f},{g['position']['y']:.0f}) | {''.join(st.get('text') or []).strip()}")
            found += 1
    print(f'{found} comment(s)')


def cmd_add(deck, spec, out):
    if out is None and K._open_in_keynote(deck):
        sys.exit('deck is open in Keynote – close it first')

    model = K.deck_model(deck)
    work = K.working_copy(deck); idx = work / 'Index'
    ids = K.IdAllocator(idx)
    comp = {str(c.get('locator') or c.get('preferredLocator')): c for c in ids.pm['components']}


    def u64():
        return str(random.getrandbits(64))


    def load_slide(n):
        p = idx / model['slides'][n - 1]['file']
        doc = K.load_yaml(p); doc['_locator'] = p.name[: -len('.iwa.yaml')]
        return p, doc


    def byid(doc):
        return {str(ai['header']['identifier']): ai for _, ai in K.archives(doc)}


    # ---- template from an existing comment
    tsl = spec.get('template_slide') or find_template_slide(model, K.unpack(deck) / 'Index')
    if not tsl:
        sys.exit('no comment in the deck to use as template – add one comment in Keynote first')
    tp, tdoc = load_slide(tsl)
    T = byid(tdoc)
    ci_id = next(i for i, ai in T.items() if K.obj_of(ai).get('_pbtype') == 'TSWP.CommentInfoArchive')
    CI = T[ci_id]; ci = K.obj_of(CI)
    CS = T[str(ci['commentStorage']['identifier'])]
    ST = T[str(ci['super']['ownedStorage']['identifier'])]
    CAP = T[str(ci['super']['super']['super']['caption']['identifier'])]
    style_refs = [str(ci['super']['super']['style']['identifier'])] + \
                 [str(e['object']['identifier']) for e in K.obj_of(ST)['tableParaStyle']['entries'] if 'object' in e] + \
                 [str(e['object']['identifier']) for e in K.obj_of(ST)['tableListStyle']['entries'] if 'object' in e]
    ss_comp = next(str(c['identifier']) for c in ids.pm['components'] if 'DocumentStylesheet' in str(c.get('locator') or c.get('preferredLocator')))

    # ---- author (create once, reuse if it exists)
    ap = idx / 'AnnotationAuthorStorage.iwa.yaml'; adoc = K.load_yaml(ap)
    store = next(K.obj_of(ai) for _, ai in K.archives(adoc) if K.obj_of(ai).get('_pbtype') == 'TSK.AnnotationAuthorStorageArchive')
    author_id = None
    for _, ai in K.archives(adoc):
        o = K.obj_of(ai)
        if o.get('_pbtype') == 'TSK.AnnotationAuthorArchive' and o.get('name') == spec['author']:
            author_id = str(ai['header']['identifier'])
    if author_id is None:
        tmpl = next(ai for _, ai in K.archives(adoc) if K.obj_of(ai).get('_pbtype') == 'TSK.AnnotationAuthorArchive')
        na = copy.deepcopy(tmpl); author_id = ids.new(); na['header']['identifier'] = author_id
        o = K.obj_of(na)
        o['name'] = spec['author']
        o['color'] = {'a': 1.0, 'r': 0.70, 'g': 0.84, 'b': 1.0, 'model': 'rgb', 'rgbspace': 'srgb'}   # light blue
        pid = f"{str(uuid.uuid4()).upper()}:{hashlib.sha256(spec['author'].encode()).hexdigest()}"
        o['publicId'] = pid; o['publicIds'] = [pid]; o['isPublicAuthor'] = False
        adoc['chunks'][0]['archives'].append(na)
        store['annotationAuthor'].append({'identifier': author_id})
        acomp = comp['AnnotationAuthorStorage']
        if 'objectUuidMapEntries' in acomp:
            acomp['objectUuidMapEntries'].append({'identifier': author_id, 'uuid': {'lower': u64(), 'upper': u64()}})
        K.save_yaml(ap, adoc)
        print('created author', spec['author'], author_id)

    # ---- comments
    docs = {}
    now = time.time() - 978307200.0   # Mac absolute time
    for c in spec['comments']:
        n = c['slide']
        if n not in docs:
            docs[n] = load_slide(n)
        p, doc = docs[n]
        A = byid(doc)
        slide_id = next(i for i, ai in A.items() if K.obj_of(ai).get('_pbtype') == 'KN.SlideArchive')
        sl_ai = A[slide_id]; sl = K.obj_of(sl_ai)
        nid = {k: ids.new() for k in ('ci', 'cap1', 'cap2', 'st', 'cs')}
        # caption standins
        cap1 = copy.deepcopy(CAP); cap1['header']['identifier'] = nid['cap1']
        cap2 = copy.deepcopy(CAP); cap2['header']['identifier'] = nid['cap2']
        # text storage
        st = copy.deepcopy(ST); st['header']['identifier'] = nid['st']; so = K.obj_of(st)
        text = c['text'].rstrip('\n') + '\n'
        so['text'] = [text]
        pe = [e for e in so['tableParaStyle']['entries'] if 'object' in e][0]
        so['tableParaStyle']['entries'] = [pe, {'characterIndex': len(text)}]
        # comment storage (author, date)
        cs = copy.deepcopy(CS); cs['header']['identifier'] = nid['cs']; co = K.obj_of(cs)
        co['author'] = {'identifier': author_id}; co['creationDate'] = {'seconds': now}
        co['storageUuid'] = {'lower': u64(), 'upper': u64()}; co['text'] = ''
        cs['header']['messageInfos'][0]['objectReferences'] = [author_id]
        # comment info drawable
        ca = copy.deepcopy(CI); ca['header']['identifier'] = nid['ci']; o = K.obj_of(ca)
        o['commentStorage'] = {'identifier': nid['cs']}
        o['super']['deprecatedStorage'] = {'identifier': nid['st']}
        o['super']['ownedStorage'] = {'identifier': nid['st']}
        g = o['super']['super']['super']
        g['caption'] = {'identifier': nid['cap1']}; g['title'] = {'identifier': nid['cap2']}
        g['parent'] = {'identifier': slide_id}
        w, h = float(c.get('w', 240)), float(c.get('h', 110))
        g['geometry']['position'] = {'x': float(c['x']), 'y': float(c['y'])}
        g['geometry']['size'] = {'width': w, 'height': h}
        bp = o['super']['super']['pathsource']['bezierPathSource']
        bp['naturalSize'] = {'width': w, 'height': h}
        bp['path']['elements'] = [{'points': [{'x': 0.0, 'y': 0.0}], 'type': 'moveTo'},
                                  {'points': [{'x': w, 'y': 0.0}], 'type': 'lineTo'},
                                  {'points': [{'x': w, 'y': h}], 'type': 'lineTo'},
                                  {'points': [{'x': 0.0, 'y': h}], 'type': 'lineTo'},
                                  {'type': 'closeSubpath'},
                                  {'points': [{'x': 0.0, 'y': 0.0}], 'type': 'moveTo'}]
        refs = ca['header']['messageInfos'][0]['objectReferences']
        ca['header']['messageInfos'][0]['objectReferences'] = [nid['cap1'], nid['cap2']] + \
            [r for r in refs if r not in (str(ci['super']['super']['super']['caption']['identifier']),
                                          str(ci['super']['super']['super']['title']['identifier']),
                                          str(ci['super']['ownedStorage']['identifier']),
                                          str(ci['commentStorage']['identifier']))] + [nid['st'], nid['cs']]
        doc['chunks'][0]['archives'] += [ca, cap1, cap2, st, cs]
        # slide: z-order, owned drawables, refs
        sl['drawablesZOrder'].append({'identifier': nid['ci']})
        sl['ownedDrawables'].append({'identifier': nid['ci']})
        sl_ai['header']['messageInfos'][0]['objectReferences'].append(nid['ci'])
        # metadata: external refs + uuids
        scomp = ids.component(K.component_id_for(doc, ids))
        have = {(str(e.get('componentIdentifier')), str(e.get('objectIdentifier'))) for e in scomp.get('externalReferences', [])}
        for compid, oid in [('2402', author_id)] + [(ss_comp, r) for r in style_refs]:
            if (compid, oid) not in have:
                scomp.setdefault('externalReferences', []).append({'componentIdentifier': compid, 'objectIdentifier': oid})
                have.add((compid, oid))
        scomp.setdefault('objectUuidMapEntries', [])
        for k in ('ci', 'st'):
            scomp['objectUuidMapEntries'].append({'identifier': nid[k], 'uuid': {'lower': u64(), 'upper': u64()}})
        print(f"slide {n}: comment {nid['ci']} at ({c['x']},{c['y']}) {w}x{h}: {text.strip()[:50]}")

    for n, (p, doc) in docs.items():
        doc.pop('_locator', None); K.save_yaml(p, doc)
    ids.save()
    print('written:', K.commit(deck, work, out))


if __name__ == '__main__':
    if len(sys.argv) < 3 or sys.argv[1] not in ('list', 'add'):
        sys.exit(__doc__)
    deck = Path(sys.argv[2])
    if sys.argv[1] == 'list':
        want = None
        if '--slides' in sys.argv:
            want = set(K.parse_range(sys.argv[sys.argv.index('--slides') + 1], 10**6))
        cmd_list(deck, want)
    else:
        out = Path(sys.argv[sys.argv.index('--out') + 1]) if '--out' in sys.argv else None
        cmd_add(deck, json.load(open(sys.argv[3])), out)
