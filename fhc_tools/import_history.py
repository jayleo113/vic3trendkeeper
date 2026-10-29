#!/usr/bin/env python3
"""Fill Full History Charts' variables in an existing save from the history the game kept.

The mod can only record from the moment it is enabled. Victoria 3 itself stores weekly graphs
for GDP, prestige, literacy, standard of living and population (radicals / loyalists for the
last 10 years). This tool samples them at 1 January of every year and writes the mod's
variables (fhc_<metric>_<slot> + first / last / prev / max, global year labels) into a NEW save,
so the in-game charts start at the beginning of the campaign.

  python import_history.py                 newest plaintext save in "save games"
  python import_history.py SAVE.v3 [--out FILE] [--install] [--dry-run]

* Runs of >= 52 identical weekly samples are engine back-fill (the country was not being
  recorded) and are treated as missing instead of drawing fake bars.
* Data the mod already recorded is kept (it wins over imported values); if imported history
  starts earlier, existing slots are renumbered.
* Only plaintext saves (SAV0100, written with -debug_mode). The source save is never modified.
"""
import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdxsave as ps  # noqa: E402

SLOTS = 150
SCALE = 100000                       # script values are stored as fixed point x100000
OUT_DIR = r'G:\Victoria3Mods\restored'
FAKE_RUN = 52
METRIC_KEYS = ('gdp', 'pop', 'gdppc', 'prestige', 'lit', 'sol', 'income', 'gold', 'debt',
               'army', 'navy', 'rad', 'loy', 'infamy')
SOURCES = {'gdp': 'gdp', 'prestige': 'prestige', 'lit': 'literacy', 'sol': 'avgsoltrend',
           'pop': 'pop_statistics/trend_population', '_rad': 'pop_statistics/trend_radicals',
           '_loy': 'pop_statistics/trend_loyalists'}
FHC_FLAG = re.compile(rb'^fhc_')


def yearly(t):
    """{year: value} from a parsed trend (channel 0), 1 January samples, back-fill removed."""
    c = t['chans'].get('0')
    if not c or not c['values']:
        return {}
    vals = c['values']
    fake = set()
    for s, n in ps.fake_runs(vals, FAKE_RUN):
        fake.update(range(s, s + n))
    step = t['rate'] * ps.HOURS_PER_TICK
    first = c['date'] - (len(vals) - 1) * step
    out = {}
    y0 = first // (365 * 24)
    for y in range(y0, c['date'] // (365 * 24) + 2):
        target = y * 365 * 24
        i = min(max(round((target - first) / step), 0), len(vals) - 1)   # newest sample may precede 1 January
        if i not in fake and abs(first + i * step - target) <= step:
            out[y] = float(vals[i])
    return out


def entry(flag, value):
    return (f'{{\n\t\t\tflag={flag}\n\t\t\tdata={{\n\t\t\t\ttype=value\n\t\t\t\tidentity={int(round(value * SCALE))}\n'
            f'\t\t\t}}\n\t\t}} ').encode()


def var_entries(buf, vstart, vend):
    """(data block span, [(flag, value or None, start, end)]) of a `variables={ data={...} }` block."""
    d = ps.get(buf, vstart + 1, vend - 1, 'data')
    if not d:
        return None, []
    items = []
    for k, kind, a, b in ps.children(buf, d[1] + 1, d[2] - 1):
        if kind != 'block':
            continue
        f = ps.get(buf, a + 1, b - 1, 'flag')
        if not f:
            continue
        flag = ps.scalar(buf, *f[1:])
        val = None
        dd = ps.get(buf, a + 1, b - 1, 'data')
        if dd:
            ty = ps.get(buf, dd[1] + 1, dd[2] - 1, 'type')
            idn = ps.get(buf, dd[1] + 1, dd[2] - 1, 'identity')
            if ty and idn and ps.scalar(buf, *ty[1:]) == 'value':
                val = int(ps.scalar(buf, *idn[1:])) / SCALE
        items.append((flag, val, a, b))
    return d, items


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('save', nargs='?')
    ap.add_argument('--out')
    ap.add_argument('--install', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
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
    print(f'存档 {os.path.basename(args.save)}（{sv.date}）')

    # global variables: existing mod state
    gpos = buf.find(b'\nvariables={')
    if gpos < 0:
        raise SystemExit('存档里没有全局 variables 块')
    gs = gpos + len('\nvariables=')
    ge = ps.block_end(buf, gs)
    gdata, gitems = var_entries(buf, gs, ge)
    gvals = {f: v for f, v, *_ in gitems}
    old_start = int(gvals['fhc_start_year']) if 'fhc_start_year' in gvals else None

    # per country: imported {metric: {year: value}} and existing mod data
    db = sv.database('country_manager')
    countries = {}
    for cid, (a, b) in db.items():
        tr = sv.trends(a, b)
        imp = {}
        for key, path in SOURCES.items():
            if path in tr:
                imp[key] = yearly(ps.parse_trend(buf, *tr[path]))
        if 'gdp' in imp and 'pop' in imp:
            imp['gdppc'] = {y: g / max(imp['pop'][y], 1) for y, g in imp['gdp'].items() if y in imp['pop']}
        for frac, src in (('rad', '_rad'), ('loy', '_loy')):
            if src in imp and 'pop' in imp:
                imp[frac] = {y: c / imp['pop'][y] for y, c in imp.pop(src).items() if imp['pop'].get(y)}
        v = ps.get(buf, a + 1, b - 1, 'variables')
        d, items = var_entries(buf, v[1], v[2]) if v else (None, [])
        existing = {}
        for flag, val, *_ in items:
            m = re.fullmatch(r'fhc_([a-z]+)_(\d+)', flag)
            if m and m.group(1) in METRIC_KEYS and val is not None and old_start is not None:
                existing.setdefault(m.group(1), {})[old_start + int(m.group(2))] = val
        countries[cid] = {'imp': imp, 'old': existing, 'data': d, 'fhc_items': [it for it in items if it[0].startswith('fhc_')]}

    years = [y for c in countries.values() for src in (c['imp'], c['old']) for ser in src.values() for y in ser]
    if not years:
        print('存档里没有可导入的历史。')
        return 0
    start = min(years)
    if old_start is not None:
        start = min(start, old_start)
    end = min(max(years), start + SLOTS - 1)
    print(f'历史范围 {start}–{end}' + (f'（mod 已有记录，起始年 {old_start}，' f'{"将重新编号" if start < old_start else "无需重新编号"}）'
                                       if old_start is not None else ''))

    edits = []
    stats = {'countries': 0, 'samples': 0, 'imported': 0}
    for cid, c in countries.items():
        if c['data'] is None:
            continue
        merged = {}
        for key in METRIC_KEYS:
            ser = dict(c['imp'].get(key, {}))
            ser.update(c['old'].get(key, {}))               # mod-recorded values win
            ser = {y: val for y, val in ser.items() if start <= y <= end}
            if ser:
                merged[key] = ser
        if not merged:
            continue
        stats['countries'] += 1
        out = []
        last_year = max(y for ser in merged.values() for y in ser)
        for key, ser in merged.items():
            ys = sorted(ser)
            for y in ys:
                out.append(entry(f'fhc_{key}_{y - start}', ser[y]))
            stats['samples'] += len(ys)
            stats['imported'] += sum(1 for y in ys if y not in c['old'].get(key, {}))
            out.append(entry(f'fhc_{key}_first', ser[ys[0]]))
            out.append(entry(f'fhc_{key}_last', ser[ys[-1]]))
            out.append(entry(f'fhc_{key}_max', max(ser.values())))
            if len(ys) > 1:
                out.append(entry(f'fhc_{key}_prev', ser[ys[-2]]))
        out.append(entry('fhc_lslot', last_year - start))
        for _flag, _val, a, b in c['fhc_items']:                # drop old fhc_* entries
            edits.append((a, b, b''))
        edits.append((c['data'][2] - 1, c['data'][2] - 1, b' ' + b''.join(out)))

    # global variables
    gout = [entry('fhc_start_year', start), entry('fhc_cur_year', end), entry('fhc_slot', end - start),
            entry('fhc_last_slot', end - start)]
    for y in range(start, end + 1):
        gout.append(entry(f'fhc_year_{y - start}', y))
    for flag, _val, a, b in gitems:
        if flag.startswith('fhc_'):
            edits.append((a, b, b''))
    edits.append((gdata[2] - 1, gdata[2] - 1, b' ' + b''.join(gout)))

    print(f"写入 {stats['countries']} 个国家，共 {stats['samples']} 个年度样本，其中新导入 {stats['imported']} 个")
    if args.dry_run:
        print('（--dry-run：未写出文件）')
        return 0
    out_path = args.out or os.path.join(OUT_DIR, os.path.splitext(os.path.basename(args.save))[0] + '_fhc.v3')
    if os.path.abspath(out_path) == os.path.abspath(args.save):
        raise SystemExit('输出文件不能和原存档相同')
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    edits.sort(key=lambda e: (e[0], e[1]))
    with open(out_path + '.tmp', 'wb') as f:
        pos = 0
        for s, e, rep in edits:
            f.write(buf[pos:s])
            f.write(rep)
            pos = e
        f.write(buf[pos:])
    os.replace(out_path + '.tmp', out_path)
    print(f'已写出 {out_path}（{os.path.getsize(out_path) / 2**20:.1f} MB）')
    if args.install:
        dst = os.path.join(ps.SAVE_DIR, os.path.basename(out_path))
        shutil.copyfile(out_path, dst)
        print(f'已放入存档目录：{dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
