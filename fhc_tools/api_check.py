"""Every GUI function / member / property used by the mod must already appear in vanilla or installed mods."""
import pathlib, re, sys
MOD = pathlib.Path(__file__).resolve().parent.parent / 'full_history_charts' / 'gui' / 'fhc_charts.gui'
REF_DIRS = [pathlib.Path(r'G:\SteamLibrary\steamapps\common\Victoria 3\game\gui'),
            pathlib.Path(r'G:\SteamLibrary\steamapps\workshop\content\529340')]
ref = []
for d in REF_DIRS:
    for p in d.rglob('*.gui'):
        try:
            ref.append(p.read_text(encoding='utf-8-sig', errors='ignore'))
        except OSError:
            pass
ref = '\n'.join(ref)
gui = MOD.read_text(encoding='utf-8-sig')
exprs = re.findall(r'"\[([^"]*)\]"|\[([^\]"]+)\]', gui)
tokens = set()
for a, b in exprs:
    e = a or b
    tokens |= set(re.findall(r'\b([A-Z][A-Za-z0-9_]*)\s*\(', e))          # functions
    tokens |= set(re.findall(r'\.([A-Z][A-Za-z0-9_]*)', e))               # members
own = set(re.findall(r'(?:type|template) ([a-z_]+)', gui))
props = set(re.findall(r'^\s*([a-z_]+)\s*=', gui, re.M)) - own
missing_t = sorted(t for t in tokens if not re.search(r'\b' + re.escape(t) + r'\b', ref))
missing_p = sorted(p for p in props if not re.search(r'^\s*' + re.escape(p) + r'\s*=', ref, re.M))
print(f'{len(tokens)} functions/members, {len(props)} properties checked')
print('missing functions/members:', missing_t or 'none')
print('missing properties:', missing_p or 'none')
sys.exit(1 if missing_t or missing_p else 0)
