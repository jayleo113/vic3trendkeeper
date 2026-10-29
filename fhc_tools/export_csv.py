#!/usr/bin/env python3
"""Export every country's yearly history from a save to CSV (for Excel / MATLAB / pandas).

  python export_csv.py                  newest plaintext save
  python export_csv.py SAVE.v3 [--out DIR]

Sources, per country and metric, in this order:
  1. the variables recorded by the Full History Charts mod (fhc_<metric>_<slot>);
  2. otherwise the game's own weekly graphs sampled at 1 January (same rules as import_history.py).

Writes two UTF-8-with-BOM files into G:\\Victoria3Mods\\exports (Excel opens them directly):
  <save>_long.csv   country_id, tag, metric, year, value, source
  <save>_<metric>.csv  one wide table per metric: rows = years, columns = country tags
"""
import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdxsave as ps  # noqa: E402
import import_history as ih  # noqa: E402

OUT_DIR = r'G:\Victoria3Mods\exports'


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('save', nargs='?')
    ap.add_argument('--out', default=OUT_DIR)
    args = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8', errors='replace')
    if not args.save:
        saves = ps.list_saves([ps.SAVE_DIR])
        if not saves:
            raise SystemExit('没有找到明文存档')
        args.save = saves[0][1]
    sv = ps.Save(args.save)
    buf = sv.buf
    gpos = buf.find(b'\nvariables={')
    gs = gpos + len('\nvariables=')
    _gdata, gitems = ih.var_entries(buf, gs, ps.block_end(buf, gs))
    gvals = {f: v for f, v, *_ in gitems}
    start = int(gvals['fhc_start_year']) if 'fhc_start_year' in gvals else None

    rows = []
    for cid, (a, b) in sv.database('country_manager').items():
        tag = '/'.join(sorted(sv.country_tags(a, b))) or cid
        mod = {}
        v = ps.get(buf, a + 1, b - 1, 'variables')
        if v and start is not None:
            _d, items = ih.var_entries(buf, v[1], v[2])
            for flag, val, *_ in items:
                m = re.fullmatch(r'fhc_([a-z]+)_(\d+)', flag)
                if m and m.group(1) in ih.METRIC_KEYS and val is not None:
                    mod.setdefault(m.group(1), {})[start + int(m.group(2))] = val
        tr = sv.trends(a, b)
        game = {}
        for key, path in ih.SOURCES.items():
            if path in tr:
                game[key] = ih.yearly(ps.parse_trend(buf, *tr[path]))
        if 'gdp' in game and 'pop' in game:
            game['gdppc'] = {y: g / max(game['pop'][y], 1) for y, g in game['gdp'].items() if y in game['pop']}
        for frac, src in (('rad', '_rad'), ('loy', '_loy')):
            if src in game and 'pop' in game:
                game[frac] = {y: c / game['pop'][y] for y, c in game.pop(src).items() if game['pop'].get(y)}
        for key in ih.METRIC_KEYS:
            ser = {y: (val, 'game') for y, val in game.get(key, {}).items()}
            ser.update({y: (val, 'mod') for y, val in mod.get(key, {}).items()})
            for y in sorted(ser):
                rows.append((cid, tag, key, y, ser[y][0], ser[y][1]))

    os.makedirs(args.out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.save))[0]
    long_path = os.path.join(args.out, f'{stem}_long.csv')
    with open(long_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['country_id', 'tag', 'metric', 'year', 'value', 'source'])
        w.writerows(rows)
    wide = {}
    for cid, tag, key, y, val, _src in rows:
        wide.setdefault(key, {}).setdefault(y, {})[tag] = val
    for key, table in wide.items():
        tags = sorted({t for yr in table.values() for t in yr})
        with open(os.path.join(args.out, f'{stem}_{key}.csv'), 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['year'] + tags)
            for y in sorted(table):
                w.writerow([y] + [table[y].get(t, '') for t in tags])
    by_src = {s: sum(1 for r in rows if r[5] == s) for s in ('mod', 'game')}
    print(f'{os.path.basename(args.save)}：{len(rows)} 行（mod 记录 {by_src["mod"]}，游戏曲线 {by_src["game"]}），'
          f'{len(wide)} 个指标表')
    print(f'已写出 {long_path} 及 {len(wide)} 个分指标表，目录 {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
