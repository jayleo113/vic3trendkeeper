#!/usr/bin/env python3
"""Repair the history graphs stored in a Victoria 3 save and write a NEW save file.

  python trend_restore.py --auto [--install]           newest save of the newest campaign
  python trend_restore.py TARGET.v3 [OLDER.v3 ...] [--out FILE] [--install]

Steps (each can be switched off):
  expand   Raise the stored capacity of graphs that Trend Keeper's `extend_saved` patch
           lengthens (country radicals/loyalists and interest-group clout -> 100 years,
           interest-group radicals/loyalists -> 10 years). Wrapped ring buffers are first
           rotated back into time order. Works whichever capacity the game honours on load.
  trim     When a country starts being recorded again (Trend Keeper's `fix_pop_stats`, or a
           country you once observed) the engine fills every skipped week with the last known
           value, drawing years of flat line. Population graphs with such runs are cut to start
           after the last one, or - when at least a year of real history precedes them - the
           runs are replaced by a straight line between the real values.
  restore  Graphs wiped by the game (switching country, forming a nation) are extended
           backwards with the history still present in OLDER saves of the same campaign;
           gaps up to --max-gap-weeks (default 26, one autosave interval) are linearly
           interpolated; longer gaps are not invented.

Only plaintext saves (header SAV0100, written with -debug_mode) are supported. The target is
never modified. Default output: G:\\Victoria3Mods\\restored\\<name>_tk.v3; --install also puts
the result into "save games" so the game can load it.
"""
import argparse
import mmap
import os
import re
import shutil
import sys

TOKEN = re.compile(rb'\s*(?:(\{)|(\})|(=)|("(?:[^"\\]|\\.)*")|([^\s={}"]+))')
SPECIAL = re.compile(rb'[{}"]')
TREND_DBS = ('country_manager', 'interest_groups', 'companies')
HOURS_PER_TICK = 6                     # sample_rate=28 ticks -> 168 h = 7 days (verified on saves)
SAVE_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'Paradox Interactive', 'Victoria 3', 'save games')
HISTORY_DIR = r'G:\Victoria3Mods\history'       # extra old saves kept off the C: drive
OUT_DIR = r'G:\Victoria3Mods\restored'
OUTPUT_SUFFIXES = ('_tk.v3', '_restored.v3')

# (database, trend path) -> {vanilla capacity: new capacity}; must match trend_keeper.extend_saved
EXPAND = {
    ('country_manager', 'pop_statistics/trend_radicals'): {521: 5214, 3650: 36500},
    ('country_manager', 'pop_statistics/trend_loyalists'): {521: 5214, 3650: 36500},
    ('interest_groups', 'clout_trend'): {521: 5214},
    ('interest_groups', 'radical_trend'): {52: 521},
    ('interest_groups', 'loyalist_trend'): {52: 521},
}
FAKE_RUN_WEEKS = 52                    # identical leading samples longer than this = engine back-fill


# ---------------------------------------------------------------- minimal plaintext-save reader
def block_end(buf, open_pos):
    """Offset just after the '}' matching the '{' at open_pos (quoted strings skipped)."""
    depth, i = 0, open_pos
    while True:
        m = SPECIAL.search(buf, i)
        if not m:
            return len(buf)
        c = buf[m.start()]
        if c == 0x22:
            j = m.end()
            while True:
                j = buf.find(b'"', j)
                if j < 0:
                    return len(buf)
                if buf[j - 1] != 0x5C:
                    break
                j += 1
            i = j + 1
            continue
        depth += 1 if c == 0x7B else -1
        if depth == 0:
            return m.end()
        i = m.end()


def children(buf, start, end):
    """Direct children of buf[start:end]: (key | None, 'block' | 'scalar', value_start, value_end)."""
    pos = start
    while pos < end:
        m = TOKEN.match(buf, pos)
        if not m or m.end() == pos or m.start() >= end or m.group(2):
            return
        pos = m.end()
        if m.group(1):
            close = block_end(buf, m.start(1))
            yield None, 'block', m.start(1), close
            pos = close
            continue
        tok = (m.group(4) or m.group(5)).decode('utf-8', 'replace')
        m2 = TOKEN.match(buf, pos)
        if m2 and m2.group(3):
            m3 = TOKEN.match(buf, m2.end())
            if m3.group(1):
                close = block_end(buf, m3.start(1))
                yield tok, 'block', m3.start(1), close
                pos = close
            else:
                yield tok, 'scalar', m3.start(4) if m3.group(4) else m3.start(5), m3.end()
                pos = m3.end()
        else:
            yield None, 'scalar', m.start(), m.end()


def get(buf, start, end, key):
    for k, kind, s, e in children(buf, start, end):
        if k == key:
            return kind, s, e
    return None


def scalar(buf, s, e):
    return bytes(buf[s:e]).strip(b'"').decode('utf-8', 'replace')


def date_hours(text):
    p = [int(x) for x in text.split('.')]
    cum = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    return ((p[0] * 365 + cum[p[1] - 1] + p[2] - 1) * 24) + (p[3] if len(p) > 3 else 0)


class Save:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        self.buf = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        if self.buf[:3] != b'SAV':
            raise SystemExit(f'{path}: 不是维多利亚3存档')
        if self.buf[5:7] != b'00':
            raise SystemExit(f'{path}: 不是明文未压缩存档（需要在 -debug_mode 下保存）')
        m = re.search(rb'game_date=([\d.]+)', self.buf[:4096])
        self.date = m.group(1).decode() if m else '?'

    def database(self, key):
        """{id: (start, end)} of the entries of a top-level `key={ database={ ... } }`."""
        pos = self.buf.find(b'\n' + key.encode() + b'={')
        if pos < 0:
            return {}
        s = pos + len(key) + 3
        db = get(self.buf, s + 1, block_end(self.buf, s) - 1, 'database')
        if not db:
            return {}
        return {k: (a, b) for k, kind, a, b in children(self.buf, db[1] + 1, db[2] - 1) if kind == 'block'}

    def trends(self, a, b, prefix=''):
        """{path: (start, end)} of trend blocks inside an entry (pop_statistics one level deeper)."""
        out = {}
        for k, kind, s, e in children(self.buf, a + 1, b - 1):
            if kind != 'block' or k is None:
                continue
            if get(self.buf, s + 1, min(e, s + 200), 'sample_rate'):
                out[prefix + k] = (s, e)
            elif k == 'pop_statistics':
                out.update(self.trends(s, e, prefix + k + '/'))
        return out

    def country_tags(self, a, b):
        d = get(self.buf, a + 1, b - 1, 'definition')
        p = get(self.buf, a + 1, b - 1, 'previous_definitions')
        tags = {scalar(self.buf, *d[1:])} if d else set()
        if p and p[0] == 'block':
            tags |= set(bytes(self.buf[p[1] + 1:p[2] - 1]).decode().split())
        return tags


def parse_trend(buf, s, e):
    """Trend header + channels with values in time order (wrapped ring buffers rotated)."""
    rate = int(scalar(buf, *get(buf, s + 1, e - 1, 'sample_rate')[1:]))
    cnt = get(buf, s + 1, e - 1, 'count')
    count = int(scalar(buf, *cnt[1:]))
    t = {'rate': rate, 'count': count, 'new_count': count, 'count_span': (cnt[1], cnt[2]), 'chans': {},
         'dirty': False, 'touched': False}
    ch = get(buf, s + 1, e - 1, 'channels')
    if ch:
        for k, kind, a, b in children(buf, ch[1] + 1, ch[2] - 1):
            d = get(buf, a + 1, b - 1, 'date')
            i = get(buf, a + 1, b - 1, 'index')
            v = get(buf, a + 1, b - 1, 'values')
            if not (d and i and v):
                continue
            vals = bytes(buf[v[1] + 1:v[2] - 1]).split()
            index = int(scalar(buf, *i[1:]))
            wrapped = len(vals) == count and index > count
            if wrapped:                                   # oldest sample sits at index % count
                p = index % count
                vals = vals[p:] + vals[:p]
            t['chans'][k] = {'date': date_hours(scalar(buf, *d[1:])), 'values': vals, 'wrapped': wrapped,
                             'orig_index': index, 'index_span': (i[1], i[2]), 'values_span': (v[1] + 1, v[2] - 1),
                             'added': 0, 'filled': 0, 'trimmed': 0}
    return t


def fmt_like(x, a, b):
    if b'.' not in a and b'.' not in b:
        return str(int(round(x))).encode()
    return (('%.5f' % x).rstrip('0').rstrip('.') or '0').encode()


def merge_channel(old, new, step, capacity, max_gap):
    """Prepend older history: returns (values, added, filled) or None."""
    nv, ov = new['values'], old['values']
    if not ov or not nv:
        return None
    n_first = new['date'] - (len(nv) - 1) * step
    o_first = old['date'] - (len(ov) - 1) * step
    k = min(len(ov), (n_first - o_first) // step)          # old samples strictly before the new graph
    if k <= 0:
        return None
    gap = round((n_first - (o_first + (k - 1) * step)) / step) - 1
    if gap > max_gap:
        return None
    a, b = ov[k - 1], nv[0]
    fa, fb = float(a), float(b)
    fill = [fmt_like(fa + (fb - fa) * (j + 1) / (gap + 1), a, b) for j in range(max(gap, 0))]
    merged = (ov[:k] + fill + nv)[-capacity:]
    return merged, len(merged) - len(nv), min(max(gap, 0), len(merged) - len(nv))


def fake_runs(vals, min_len=FAKE_RUN_WEEKS):
    """[(start, length)] of runs of identical samples >= min_len.

    A real population changes almost every week; such runs are the engine filling skipped weeks
    with the last known value when a country starts being recorded again."""
    runs, i = [], 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[i]:
            j += 1
        if j - i + 1 >= min_len:
            runs.append((i, j - i + 1))
        i = j + 1
    return runs


def backfill_plan(vals):
    """('trim', keep_last_n) | ('bridge', [(start, length)]) | None for a population graph."""
    runs = fake_runs(vals)
    if not runs or runs[-1][0] + runs[-1][1] >= len(vals):
        return None                                   # nothing fake, or no real data after it yet
    last_end = runs[-1][0] + runs[-1][1]
    # longest stretch of genuinely changing samples before the last fake run
    edges = [0] + [x for s, n in runs for x in (s, s + n)]
    real_before = max((edges[k + 1] - edges[k] for k in range(0, len(edges) - 1, 2)), default=0)
    if real_before < FAKE_RUN_WEEKS:
        return 'trim', len(vals) - last_end
    lead = [r for r in runs if r[0] == 0]
    if lead:                                          # never bridge from nothing: drop a fake start
        return 'trim', len(vals) - lead[0][1]
    return 'bridge', runs


def interpolate(vals, s, n):
    """Replace vals[s:s+n] by a straight line between its real neighbours; returns samples changed."""
    s0, s1 = max(s, 1), min(s + n, len(vals) - 1)
    if s1 <= s0:
        return 0
    a, b = vals[s0 - 1], vals[s1]
    fa, fb = float(a), float(b)
    for k in range(s0, s1):
        vals[k] = fmt_like(fa + (fb - fa) * (k - s0 + 1) / (s1 - s0 + 1), a, b)
    return s1 - s0


# ---------------------------------------------------------------- campaign discovery
def list_saves(dirs):
    out = []
    for d in dirs:
        for name in os.listdir(d) if os.path.isdir(d) else []:
            p = os.path.join(d, name)
            if not name.endswith('.v3') or name.endswith(OUTPUT_SUFFIXES):
                continue
            with open(p, 'rb') as f:
                head = f.read(16384)
            m = re.search(rb'playthrough_id="([^"]+)"', head)
            if head[:3] == b'SAV' and head[5:7] == b'00' and m:
                out.append((os.path.getmtime(p), p, m.group(1)))
    return sorted(out, reverse=True)


def pick_campaign(dirs):
    saves = list_saves(dirs)
    if not saves:
        raise SystemExit('没有找到明文存档（需要 -debug_mode 下保存的存档）')
    _, target, pid = saves[0]
    older = [p for _, p, q in saves[1:] if q == pid]
    print(f'自动选择：本局（playthrough {pid.decode()}）共 {len(older) + 1} 份存档，最新的是 {os.path.basename(target)}')
    return target, older


# ---------------------------------------------------------------- main
def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('target', nargs='?')
    ap.add_argument('older', nargs='*')
    ap.add_argument('--auto', action='store_true')
    ap.add_argument('--dirs', nargs='*', default=[SAVE_DIR, HISTORY_DIR])
    ap.add_argument('--out')
    ap.add_argument('--install', action='store_true', help='also copy the result into "save games"')
    ap.add_argument('--no-expand', action='store_true')
    ap.add_argument('--no-trim', action='store_true')
    ap.add_argument('--max-gap-weeks', type=int, default=26)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8', errors='replace')
    if args.auto:
        args.target, args.older = pick_campaign(args.dirs)
    elif not args.target:
        ap.error('需要 TARGET，或使用 --auto')

    tgt = Save(args.target)
    print(f'目标存档 {os.path.basename(args.target)}（{tgt.date}）')
    trends, tags = {}, {}
    for db in TREND_DBS:
        for oid, (a, b) in tgt.database(db).items():
            if db == 'country_manager':
                tags[oid] = tgt.country_tags(a, b)
            for path, (s, e) in tgt.trends(a, b).items():
                trends[(db, oid, path)] = parse_trend(tgt.buf, s, e)
    print(f'  共 {len(trends)} 条曲线')
    stats = {'expanded': 0, 'unwrapped': 0, 'trimmed_countries': [], 'trimmed_samples': 0,
             'bridged_countries': [], 'bridged_samples': 0}

    # 1. expand capacities
    if not args.no_expand:
        for (db, oid, path), t in trends.items():
            new = EXPAND.get((db, path), {}).get(t['count'])
            if new:
                t['new_count'], t['dirty'] = new, True
                stats['expanded'] += 1
                stats['unwrapped'] += sum(c['wrapped'] for c in t['chans'].values())

    # 2. the engine's flat back-fill in population graphs (see fake_runs): trim or interpolate
    if not args.no_trim:
        for oid in tags:
            pop = trends.get(('country_manager', oid, 'pop_statistics/trend_population'))
            c = pop and pop['chans'].get('0')
            if not c:
                continue
            plan = backfill_plan(c['values'])
            if not plan:
                continue
            kind, arg = plan
            L = len(c['values'])
            group = [t for (db, o, path), t in trends.items()
                     if db == 'country_manager' and o == oid and path.startswith('pop_statistics/') and t['rate'] == pop['rate']]
            for t in group:
                t['dirty'] = t['touched'] = True
                for ch in t['chans'].values():
                    if kind == 'trim':                       # keep only what follows the last fake run
                        if len(ch['values']) > arg:
                            ch['trimmed'] = len(ch['values']) - arg
                            ch['values'] = ch['values'][-arg:]
                    else:                                    # bridge fake runs between real history
                        shift = L - len(ch['values'])        # graphs are aligned at their newest sample
                        for s, n in arg:
                            ch['filled'] += interpolate(ch['values'], s - shift, n)
            if kind == 'trim':
                stats['trimmed_countries'].append('/'.join(sorted(tags[oid])))
                stats['trimmed_samples'] += L - arg
            else:
                stats['bridged_countries'].append('/'.join(sorted(tags[oid])))
                stats['bridged_samples'] += sum(n for _, n in arg)

    # 3. restore wiped history from older saves (newest first; each only adds what is older)
    olds = sorted((Save(p) for p in args.older), key=lambda s: date_hours(s.date) if s.date != '?' else 0, reverse=True)
    for old in olds:
        print(f'读取旧存档 {os.path.basename(old.path)}（{old.date}）……')
        for db in TREND_DBS:
            for oid, (a, b) in old.database(db).items():
                if db == 'country_manager' and oid in tags and not (old.country_tags(a, b) & tags[oid]):
                    continue                                 # id reused by another country
                for path, (s, e) in old.trends(a, b).items():
                    t = trends.get((db, oid, path))
                    if not t or t['touched']:                  # back-filled graphs: older saves hold the same fakes
                        continue
                    ot = parse_trend(old.buf, s, e)
                    if ot['rate'] != t['rate']:
                        continue
                    for ck, oc in ot['chans'].items():
                        c = t['chans'].get(ck)
                        if not c:
                            continue
                        r = merge_channel(oc, c, t['rate'] * HOURS_PER_TICK, t['new_count'], args.max_gap_weeks)
                        if r:
                            c['values'], add, fill = r
                            c['added'] += add
                            c['filled'] += fill
                            t['dirty'] = True

    # report
    dirty = {k: t for k, t in trends.items() if t['dirty']}
    if stats['expanded']:
        print(f"扩容：{stats['expanded']} 条曲线（其中 {stats['unwrapped']} 条已循环覆盖，已还原时间顺序）")
    if stats['trimmed_countries']:
        print(f"修剪伪平线：{len(stats['trimmed_countries'])} 个国家的人口曲线改为从首个真实样本开始，删除 "
              f"{stats['trimmed_samples']} 个填充样本（如 {', '.join(stats['trimmed_countries'][:8])}）")
    if stats['bridged_countries']:
        print(f"桥接伪平线：{len(stats['bridged_countries'])} 个国家保留真实早期历史，{stats['bridged_samples']} 个填充样本"
              f"改为线性插值（如 {', '.join(stats['bridged_countries'][:8])}）")
    restored = [(k, c) for k, t in dirty.items() for c in t['chans'].values() if c['added']]
    by_db = {}
    for (db, oid, path), c in restored:
        by_db.setdefault(db, []).append((oid, path, c['added'], c['filled']))
    for db, rows in by_db.items():
        print(f'恢复 {db}：{len(rows)} 条曲线，补回 {sum(r[2] for r in rows)} 个采样（其中插值 {sum(r[3] for r in rows)}）')
        for oid, path, add, fill in sorted(rows, key=lambda r: -r[2])[:6]:
            label = '/'.join(sorted(tags.get(oid, []))) if db == 'country_manager' else ''
            print(f'      {oid:>10} {label:8s} {path:44s} +{add}（插值 {fill}）')
    if not dirty:
        print('没有需要修改的曲线。')
        return 0
    if args.dry_run:
        print('（--dry-run：未写出文件）')
        return 0

    # write
    out = args.out or os.path.join(OUT_DIR, os.path.splitext(os.path.basename(args.target))[0] + '_tk.v3')
    if os.path.abspath(out) == os.path.abspath(args.target):
        raise SystemExit('输出文件不能和目标存档相同')
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    edits = []
    for t in dirty.values():
        if t['new_count'] != t['count']:
            edits.append((*t['count_span'], str(t['new_count']).encode()))
        for c in t['chans'].values():
            edits.append((*c['index_span'], str(len(c['values'])).encode()))
            edits.append((*c['values_span'], b' ' + b' '.join(c['values']) + b' '))
    edits.sort()
    with open(out + '.tmp', 'wb') as f:
        pos = 0
        for s, e, rep in edits:
            f.write(tgt.buf[pos:s])
            f.write(rep)
            pos = e
        f.write(tgt.buf[pos:])
    os.replace(out + '.tmp', out)
    print(f'已写出 {out}（{os.path.getsize(out) / 2**20:.1f} MB）')
    if args.install:
        dst = os.path.join(SAVE_DIR, os.path.basename(out))
        shutil.copyfile(out, dst)
        print(f'已放入存档目录：{dst}（游戏内「读取游戏」即可选择）')
    else:
        print('需要在游戏里读取时，把它复制到 文档\\Paradox Interactive\\Victoria 3\\save games\\，或加 --install。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
