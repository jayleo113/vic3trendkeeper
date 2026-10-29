#!/usr/bin/env python3
"""Static checks for the generated mod: brace balance, macro parameters, localization keys."""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'full_history_charts'
ok = True


def fail(msg):
    global ok
    ok = False
    print('FAIL', msg)


def strip_strings_comments(text):
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    return re.sub(r'#[^\n]*', '', text)


for p in list(ROOT.rglob('*.txt')) + list(ROOT.rglob('*.gui')):
    if 'tools' in p.parts:
        continue
    raw = p.read_bytes()
    if p.suffix in ('.txt', '.gui') and 'scripted_widgets' not in p.parts and not raw.startswith(b'\xef\xbb\xbf'):
        fail(f'{p.name}: missing UTF-8 BOM')
    t = strip_strings_comments(raw.decode('utf-8-sig'))
    depth = 0
    for ch in t:
        depth += (ch == '{') - (ch == '}')
        if depth < 0:
            break
    if depth != 0:
        fail(f'{p.name}: unbalanced braces ({depth})')
    else:
        print('ok  braces', p.relative_to(ROOT))

eff = (ROOT / 'common/scripted_effects/fhc_effects.txt').read_text(encoding='utf-8-sig')
blocks = re.findall(r'^(\w+) = \{(.*?)^\}', eff, re.S | re.M)
for name, body in blocks:
    uses = '$S$' in body
    callers = re.findall(rf'{name} = \{{ S = ', eff)
    if uses and not callers:
        fail(f'{name} uses $S$ but is never called with S')
    if not uses and callers:
        fail(f'{name} called with S but does not use it')
print('ok  macro parameters' if ok else '', len(blocks), 'effects')

gui = (ROOT / 'gui/fhc_charts.gui').read_text(encoding='utf-8-sig')
keys = set(re.findall(r'text = "(FHC_[A-Za-z_]+)"', gui)) | set(re.findall(r"Localize\('(FHC_[A-Za-z_]+)'\)", gui)) \
    | set(re.findall(r'tooltip = "(FHC_[A-Za-z_]+)"', gui))
for lang in ('simp_chinese', 'english'):
    loc = (ROOT / f'localization/{lang}/fhc_l_{lang}.yml').read_text(encoding='utf-8-sig')
    have = set(re.findall(r'^ (FHC_[A-Za-z_]+):', loc, re.M))
    missing = keys - have
    if missing:
        fail(f'{lang}: missing {sorted(missing)}')
    else:
        print(f'ok  localization {lang}: {len(keys)} keys')
widgets = (ROOT / 'gui/scripted_widgets/fhc_widgets.txt').read_text(encoding='utf-8-sig')
for path, name in re.findall(r'(\S+) = (\S+)', widgets):
    if not (ROOT / path).exists() or f'name = "{name}"' not in gui:
        fail(f'scripted widget {path} = {name} not found')
print('ok  scripted widgets')


# --- simulate the generated binary searches ---------------------------------
def block_of(text, name):
    i = text.index(f'\n{name} = {{') + 1
    j, depth = text.index('{', i), 0
    for k in range(j, len(text)):
        depth += (text[k] == '{') - (text[k] == '}')
        if depth == 0:
            return re.sub(r'#[^\n]*', '', text[j + 1:k])


def run_tree(body, x, cond_re, leaf_re):
    """Walk nested `if = { limit = { <cond> } ... } else = { ... }` for value x; return the leaf value."""
    while True:
        body = body.strip()
        m = re.match(r'if = \{\s*limit = \{ ' + cond_re + r' \}', body)
        if not m:
            leaf = re.search(leaf_re, body)
            return int(leaf.group(1)) if leaf else None
        k = int(m.group(1))
        # split if-body / else-body by brace matching
        start = body.index('{')
        depth = 0
        for end in range(start, len(body)):
            depth += (body[end] == '{') - (body[end] == '}')
            if depth == 0:
                break
        if_body = body[m.end():end]
        rest = body[end + 1:].strip()
        assert rest.startswith('else = {'), rest[:40]
        else_body = rest[rest.index('{') + 1:rest.rindex('}')]
        body = if_body if x >= k else else_body


year_body = block_of(eff, 'fhc_compute_year')
bad = [y for y in range(1400, 2501) if run_tree(year_body, y, r'game_date >= (\d+)\.1\.1', r'value = (\d+)') != y]
if bad:
    fail(f'year search wrong for {bad[:5]}')
else:
    print('ok  year binary search: all 1101 years map to themselves')
rec = block_of(eff, 'fhc_record_year')
slot_body = rec[rec.index('limit = { global_var:fhc_slot >= 0'):]
slot_body = slot_body[slot_body.index('}') + 1:slot_body.rindex('}')]
bad = [s for s in range(150) if run_tree(slot_body, s, r'global_var:fhc_slot >= (\d+)', r'S = (\d+)') != s]
if bad:
    fail(f'slot search wrong for {bad[:5]}')
else:
    print('ok  slot binary search: all 150 slots map to themselves')
depth = max(len(re.findall(r'if = \{', l)) for l in [year_body])
print('ALL OK' if ok else 'FAILED')
sys.exit(0 if ok else 1)
