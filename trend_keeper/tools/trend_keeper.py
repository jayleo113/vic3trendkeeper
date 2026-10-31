#!/usr/bin/env python3
"""Trend Keeper - runtime switch for Victoria 3 history-graph fixes.

Patches the *running* victoria3.exe in memory (the file on disk is never modified), so
every fix can be turned on and off while the game is open. Patch sites are located by
byte signatures in the on-disk exe and every original byte is verified before writing;
anything unexpected puts the helper in read-only mode.

Root causes (reverse engineered from Victoria 3 1.14.5):

  keep_history   SetCountryTrendStatus(country, 0) runs when a country stops being tracked
                 (you switch away from it, it changes tag / forms a nation, an observed
                 country is released). It calls ClearCountryTrends(), which after some
                 bookkeeping wipes the country's GDP, prestige, SoL (all strata), state,
                 building, company, market and interest-group graphs.
                 Patch: keep the bookkeeping, jump over the trend-wiping part (via a 13-byte
                 code cave in int3 padding) and still perform its one non-trend write.

  fix_pop_stats  RecalcCountryPopStatistics (a weekly tick task) only pushes a country's
                 population / strata / radicals / loyalists graphs when
                 IsTrendUpdateDay(country) is true. For AI countries that is
                 "country_id % 7 == day % 7" - but the task itself runs every 7 days, so the
                 day term never changes and ONLY countries whose id is divisible by 7 ever get
                 population graphs (verified in saves: BUK 105, CAN 175, BRZ 203, LIT 33555249
                 ...). Patch: treat that single call site as always true.

  track_pops     (advanced, default off) per-pop graphs (pop tooltips) are only recorded for
                 tracked countries. Enabling it for all ~180k pops costs memory that grows
                 with every game week.

Trends are presentation-only data ("Trends must be used only for the visual presentation");
none of the patches touches simulation, AI, checksums or multiplayer sync.

Usage:
  pythonw trend_keeper.py              GUI (default)
  python  trend_keeper.py status       attach (read-only) and show patch state
  python  trend_keeper.py on [NAME..] | off [NAME..]
  python  trend_keeper.py selftest     resolve every patch against the exe on disk (no game needed)
  python  trend_keeper.py bench SECONDS
  python  trend_keeper.py compare SECONDS [NAME..]   A/B/A benchmark (default: fix_pop_stats)
  python  trend_keeper.py profile [SECONDS [MS]]    read-only hotspot sampler (is the lag in autonomous investment?)
  python  trend_keeper.py invest [set BUDGET [MIN] | reset]   cap_investment stats / live parameters (no restart)
  python  trend_keeper.py daemon       headless: auto-apply, hotkey, in-game button
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import struct
import sys
import threading
import time

GAME_EXE = 'victoria3.exe'
TESTED_VERSION = '1.14.5'
DEFAULT_EXE = r'G:\SteamLibrary\steamapps\common\Victoria 3\binaries\victoria3.exe'
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, 'trend_keeper_config.json')
LOG_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'Paradox Interactive', 'Victoria 3', 'logs')
MARKER = 'TRENDKEEPER:'


# ================================================================ exe image + signatures
class Image:
    """The on-disk exe, used to locate patch sites (memory may already be patched)."""

    def __init__(self, path):
        with open(path, 'rb') as f:
            raw = f.read()
        self.raw = raw
        pe = struct.unpack_from('<I', raw, 0x3C)[0]
        nsec = struct.unpack_from('<H', raw, pe + 6)[0]
        opt = pe + 24
        self.base = struct.unpack_from('<Q', raw, opt + 24)[0]
        self.image_size = struct.unpack_from('<I', raw, opt + 56)[0]
        exc_rva, exc_size = struct.unpack_from('<II', raw, opt + 112 + 3 * 8)
        sh = opt + struct.unpack_from('<H', raw, pe + 20)[0]
        self.secs = []
        for i in range(nsec):
            name = raw[sh + 40 * i: sh + 40 * i + 8].rstrip(b'\0').decode()
            vsize, va, rsize, rptr = struct.unpack_from('<IIII', raw, sh + 40 * i + 8)
            self.secs.append((name, va, vsize, rptr, rsize))
        _, self.text_rva, _, trp, trs = next(s for s in self.secs if s[0] == '.text')
        self.code = raw[trp:trp + trs]
        off = self.rva2off(exc_rva)
        self.funcs = sorted(struct.unpack_from('<II', raw, off + 12 * i) for i in range(exc_size // 12))
        self.fstarts = [f[0] for f in self.funcs]

    def rva2off(self, rva):
        for _, va, vs, rp, rs in self.secs:
            if va <= rva < va + max(vs, rs):
                return rp + rva - va
        raise ValueError(hex(rva))

    def bytes_at(self, rva, n):
        o = self.rva2off(rva)
        return self.raw[o:o + n]

    def find(self, pattern, start_rva=None, end_rva=None):
        """RVAs where a '48 8b ?? ..' pattern matches inside .text (optionally within [start, end))."""
        rx = re.compile(b''.join(b'.' if t == '??' else re.escape(bytes([int(t, 16)])) for t in pattern.split()), re.S)
        s = 0 if start_rva is None else start_rva - self.text_rva
        e = len(self.code) if end_rva is None else end_rva - self.text_rva
        return [self.text_rva + m.start() for m in rx.finditer(self.code, s, e)]

    def find_one(self, pattern, what, **kw):
        hits = self.find(pattern, **kw)
        if len(hits) != 1:
            raise LookupError(f'{what}: 特征码匹配 {len(hits)} 处（应为 1），游戏版本可能不同')
        return hits[0]

    def in_function(self, rva):
        import bisect
        i = bisect.bisect_right(self.fstarts, rva) - 1
        return i >= 0 and self.funcs[i][0] <= rva < self.funcs[i][1]

    def func_range(self, rva):
        import bisect
        i = bisect.bisect_right(self.fstarts, rva) - 1
        if i < 0 or not self.funcs[i][0] <= rva < self.funcs[i][1]:
            raise LookupError(f'{rva:#x} 不在任何函数内')
        return self.funcs[i]

    def code_cave(self, size):
        """16-byte aligned spot inside an int3 padding run that belongs to no function (never handed out twice)."""
        used = self.__dict__.setdefault('_used_caves', [])
        for m in re.finditer(rb'\xcc{%d,}' % (size + 16), self.code):
            start, end = self.text_rva + m.start(), self.text_rva + m.end()
            cave = (start + 8 + 15) & ~15
            if cave + size <= end - 4 and not self.in_function(start) and not self.in_function(end - 1):
                if any(cave < u + n and u < cave + size for u, n in used):
                    continue
                used.append((cave, size))
                return cave
        raise LookupError('找不到可用的代码洞')


    def extra_cave(self):
        """RVA of a block the helper allocates itself right behind the image (rel32-reachable); the exe has no room left."""
        return (self.image_size + 0xFFFF + 0x10000) & ~0xFFFF


def rel32(src_next, dst):
    return struct.pack('<i', dst - src_next)


def resolve_keep_history(img):
    site = img.find_one('49 8d 8e 70 22 00 00 e8 ?? ?? ?? ?? 49 8d 8e a8 22 00 00 e8 ?? ?? ?? ?? '
                        '49 8d 8e 18 23 00 00 e8 ?? ?? ?? ?? 49 8d 8e 50 23 00 00 e8', 'ClearCountryTrends')
    # the first calls must all go to the same function (CTrend::Clear)
    tgts = {site + o + 5 + struct.unpack('<i', img.bytes_at(site + o + 1, 4))[0] for o in (7, 19, 31)}
    if len(tgts) != 1:
        raise LookupError('ClearCountryTrends: 调用目标不一致')
    epi = img.find_one('48 8b 5c 24 60 48 8b 6c 24 68 48 83 c4 20 41 5f 41 5e 41 5d 5f 5e c3',
                       'ClearCountryTrends 尾声', start_rva=site, end_rva=site + 0x600)
    flag = img.find_one('41 c6 86 ?? ?? 00 00 00', 'ClearCountryTrends 标志写入', start_rva=site, end_rva=epi)
    cave = img.code_cave(13)
    cave_code = img.bytes_at(flag, 8) + b'\xe9' + rel32(cave + 13, epi)    # mov byte [r14+X],0 ; jmp epilogue
    jmp = b'\xe9' + rel32(site + 5, cave)
    return [(cave, b'\xcc' * 13, cave_code, True),                         # (rva, original, patched, is_cave)
            (site, img.bytes_at(site, 5), jmp, False)]


def resolve_fix_pop_stats(img):
    a = img.find_one('48 8b 98 08 06 00 00 e8 ?? ?? ?? ?? 84 c0 74 ?? 48 8b 53 08 48 8d 8d 18 0d 00 00 e8',
                     'RecalcCountryPopStatistics')
    site = a + 7
    return [(site, img.bytes_at(site, 5), b'\xb0\x01\x0f\x1f\x00', False)]  # call IsTrendUpdateDay -> mov al,1 ; nop3


def resolve_track_pops(img):
    a = img.find_one('48 8b 07 44 8b 48 18 41 8b c9 81 e1 ff ff ff 00 41 3b 8e ec 00 00 00 73 ?? '
                     '49 8b 86 e0 00 00 00 8b 14 88 41 3b d4 74 ?? 83 fa 02 75 ?? 41 3b 8d cc 16 00 00 73 ?? '
                     '49 8b 85 c0 16 00 00 44 39 0c c8 75 ?? 48 63 4c c8 04 49 8b 85 d8 16 00 00 48 c1 e1 05 '
                     '48 03 c1 80 78 18 00 0f 84', 'UpdatePopTrends')
    skip = a + 0x19 + struct.unpack('<b', img.bytes_at(a + 0x18, 1))[0]    # loop "continue" target
    lookup = a + 0x2d                                                       # record lookup after the status check
    s1, s2, s3, s4 = a + 0x10, a + 0x34, a + 0x41, a + 0x5a
    return [
        (s1, img.bytes_at(s1, 2), b'\xeb' + struct.pack('<b', lookup - (s1 + 2)), False),   # skip status check
        (s2, img.bytes_at(s2, 2), b'\x73' + struct.pack('<b', skip - (s2 + 2)), False),     # no record -> skip
        (s3, img.bytes_at(s3, 2), b'\x75' + struct.pack('<b', skip - (s3 + 2)), False),     # stale     -> skip
        (s4, img.bytes_at(s4, 6), b'\x0f\x84' + rel32(s4 + 6, skip), False),                # invalid   -> skip
    ]


YEARS_100, YEARS_10 = 0x23A50, 0x3908       # trend length in 6-hour ticks (146000 = 100 years)


def imm_site(img, sig, what, new_imm):
    """`<12 bytes> 41 b8 <imm32> e8`: replace the imm32 of `mov r8d, imm32` (trend length argument)."""
    a = img.find_one(sig, what)
    return (a + 14, img.bytes_at(a + 14, 4), struct.pack('<I', new_imm), False)


def resolve_extend_saved(img):
    sites = [
        # CPopStatistics ctor: country radicals / loyalists, 10 -> 100 years
        ('48 8d 8b 18 04 00 00 ba 1c 00 00 00 41 b8 08 39 00 00 e8', 'country radicals', YEARS_100),
        ('48 8d 8b 50 04 00 00 ba 1c 00 00 00 41 b8 08 39 00 00 e8', 'country loyalists', YEARS_100),
        # same trends when a country becomes tracked (daily) / untracked (weekly) - otherwise they shrink back
        ('48 8d 8b 18 04 00 00 ba 04 00 00 00 41 b8 08 39 00 00 e8', 'tracked radicals', YEARS_100),
        ('48 8d 8b 50 04 00 00 ba 04 00 00 00 41 b8 08 39 00 00 e8', 'tracked loyalists', YEARS_100),
        ('49 8d 8e 30 11 00 00 ba 1c 00 00 00 41 b8 08 39 00 00 e8', 'untracked radicals', YEARS_100),
        ('49 8d 8e 68 11 00 00 ba 1c 00 00 00 41 b8 08 39 00 00 e8', 'untracked loyalists', YEARS_100),
        # CInterestGroup ctor: clout 10 -> 100 years; radicals / loyalists 1 -> 10 years
        ('49 8d 8e f8 00 00 00 ba 1c 00 00 00 41 b8 08 39 00 00 e8', 'IG clout', YEARS_100),
        ('49 8d 8e 30 01 00 00 ba 1c 00 00 00 41 b8 b4 05 00 00 e8', 'IG radicals', YEARS_10),
        ('49 8d 8e 68 01 00 00 ba 1c 00 00 00 41 b8 b4 05 00 00 e8', 'IG loyalists', YEARS_10),
    ]
    return [imm_site(img, sig, what, imm) for sig, what, imm in sites]


def resolve_extend_budget(img):
    first = img.find_one('48 8d 8b 38 03 00 00 ba 1c 00 00 00 41 b8 b4 05 00 00 e8', 'CBudget ctor')
    init = first + 23 + struct.unpack('<i', img.bytes_at(first + 19, 4))[0]   # trend Init(): call rel32 after the sig
    f0, f1 = img.func_range(first)
    sites = []
    for a in img.find('41 b8 b4 05 00 00 e8', start_rva=f0, end_rva=f1):
        if a + 11 + struct.unpack('<i', img.bytes_at(a + 7, 4))[0] == init:      # only `mov r8d,1y ; call Init`
            sites.append((a + 2, img.bytes_at(a + 2, 4), struct.pack('<I', YEARS_100), False))
    if len(sites) < 20:
        raise LookupError(f'CBudget ctor: 只找到 {len(sites)} 条预算曲线')
    return sites

INVEST_BUDGET = 20000   # work units (placements x potential buildings) a country may spend per weekly task
INVEST_MIN = 3          # never fewer than this many placements per country per task
EXTRA_SIZE = 0x1000
INV_DATA = 0x100        # offset of the data area inside the helper-allocated block (code sits at offset 0)
INV_FIELDS = ('budget', 'min', 'calls', 'throttled', 'max_p', 'max_n')   # u32 each, from INV_DATA


def _asm_invest(cave, site):
    """45+ bytes of x64 for the cap, RIP-relative to the data area, hand assembled (no assembler dependency).

    cmp edx,ebx ; cmovg ebx,edx            the two replaced instructions: ebx = n = max(1, pool/cost)
    mov ecx,[r14+0x21c]                    P = number of potential buildings of this country
    test ecx,ecx ; jle done
    inc [calls] ; max_p = max(max_p, P) ; max_n = max(max_n, n)
    mov eax,[budget] ; xor edx,edx ; div ecx ; cmp eax,[min] ; jae ok ; mov eax,[min]     cap = max(min, budget/P)
    ok: cmp ebx,eax ; jle done ; inc [throttled] ; mov ebx,eax
    done: jmp site+5"""
    out = bytearray()
    labels, fixups = {}, []

    def emit(b):
        out.extend(bytes.fromhex(b) if isinstance(b, str) else b)

    def rip(opcode_hex, field):                  # opcode + modrm(rip) + disp32 to the data field
        emit(opcode_hex)
        fixups.append((len(out), field))
        emit(b'\0\0\0\0')

    def jcc(opcode_hex, label):
        emit(opcode_hex)
        fixups.append((len(out), label))
        emit(b'\0')

    emit('3bd30f4fda'); emit('418b8e1c020000'); emit('85c9'); jcc('7e', 'done')
    rip('ff05', 'calls')
    rip('3b0d', 'max_p'); jcc('76', 'p_ok'); rip('890d', 'max_p'); labels['p_ok'] = len(out)
    rip('3b1d', 'max_n'); jcc('76', 'n_ok'); rip('891d', 'max_n'); labels['n_ok'] = len(out)
    rip('8b05', 'budget'); emit('31d2f7f1'); rip('3b05', 'min'); jcc('73', 'ok'); rip('8b05', 'min')
    labels['ok'] = len(out)
    emit('39c3'); jcc('7e', 'done'); rip('ff05', 'throttled'); emit('89c3')
    labels['done'] = len(out)
    emit('e9'); jmp_at = len(out); emit(b'\0\0\0\0')
    for at, target in fixups:
        if target in labels:                      # rel8
            out[at] = labels[target] - (at + 1)
        else:                                     # rip-relative disp32; next instruction starts at at+4
            disp = cave + INV_DATA + 4 * INV_FIELDS.index(target) - (cave + at + 4)
            out[at:at + 4] = struct.pack('<i', disp)
    out[jmp_at:jmp_at + 4] = rel32(cave + jmp_at + 4, site + 5)
    return bytes(out)


def resolve_cap_investment(img):
    """HandleAutonomousInvestment serial phase (0x140f771a0): n = max(1, round(pool / cost)).

    Every placement re-scans all P potential buildings (SetPotentialPrivateInvestmentPriorities), so the cost of one
    country is about n x P. Cap n at clamp(BUDGET / P, MIN, n): small countries are untouched, only huge ones are
    throttled. The first `cap` placements are exactly the vanilla ones; the rest wait for the next weekly task.
    BUDGET / MIN and some counters live in the data area, so they can be changed while the game runs."""
    site = img.find_one('3b d3 0f 4f da 85 db 0f 8e ?? ?? ?? ??', 'HandleAutonomousInvestment 循环次数')
    cave = img.extra_cave()
    code = _asm_invest(cave, site)
    assert len(code) < INV_DATA
    jmp = bytes.fromhex('e9') + rel32(site + 5, cave)
    return [(cave, bytes(len(code)), code, 2),                   # 2 = block allocated by the helper
            (site, img.bytes_at(site, 5), jmp, False)]


PATCHES = {
    'keep_history': {
        'title': '切换/成立国家后保留历史曲线',
        'desc': '离开一个国家、或它改 tag / 成立新国家时，不再清空它的 GDP、声望、生活水平、州、建筑、公司、利益集团等曲线。',
        'resolve': resolve_keep_history, 'default': True,
    },
    'fix_pop_stats': {
        'title': '所有国家记录人口曲线（修复原版 ID%7 bug）',
        'desc': '原版只有国家 ID 能被 7 整除的 AI 才记录人口/阶层/激进/忠诚曲线。修复后所有国家每周记录，观察者模式可看全时间线。',
        'resolve': resolve_fix_pop_stats, 'default': True,
    },
    'extend_saved': {
        'title': '延长存档曲线：激进/忠诚人口、利益集团影响力 → 100 年',
        'desc': '原版只保留 10 年（利益集团激进/忠诚只有 1 年，延长到 10 年）。内存约 +48 MB，存档每年约 +0.5 MB。'
                '对之后创建/读档的对象生效；旧存档先用「恢复存档历史.bat」扩容。',
        'resolve': resolve_extend_saved, 'default': True,
    },
    'extend_budget': {
        'title': '延长预算曲线：1 年 → 100 年',
        'desc': '预算各项收支曲线原版只保留 1 年。内存约 +135 MB，不进存档（原版设计，读档后从读档日起记录）。'
                '对之后读档/开局生效。',
        'resolve': resolve_extend_budget, 'default': True,
    },
    'cap_investment': {
        'title': '（高级）限制自主投资每周落地次数（缓解后期卡顿）',
        'desc': '自主投资每落地一笔都会重扫该国全部潜在建筑，开销约 落地笔数×潜在建筑数。开启后每国每周最多落地 '
                'max(最小值, 预算÷潜在建筑数) 笔（默认预算 20000、最小 3；配置项 invest_budget / invest_min）：小国不受影响，'
                '只限制超大建造力的国家，超出的资金留到下周。前几笔与原版相同，但会改变模拟结果，联机必须所有人同样开启。默认关闭。',
        'resolve': resolve_cap_investment, 'default': False,
    },
    'track_pops': {
        'title': '（高级）所有国家的单个 pop 曲线',
        'desc': 'pop 悬浮提示里的曲线对全部国家记录。约 18 万个 pop，内存随游戏时间持续增长，默认关闭。',
        'resolve': resolve_track_pops, 'default': False,
    },
}
ORDER = ['keep_history', 'fix_pop_stats', 'extend_saved', 'extend_budget', 'cap_investment', 'track_pops']
DEFAULT_CONFIG = {'enabled': {n: PATCHES[n]['default'] for n in ORDER}, 'auto_apply': True, 'hotkey': True}

# CTrendManager::Update reads the game state as [[rip+X]+0x608]; its +0x8 is the clock in hours
GLOBAL_SIG = '48 8b 05 ?? ?? ?? ?? 49 8d 4f 10 4c 8b b0 08 06 00 00'

_resolved = {}


def resolve_all(exe_path):
    """({name: [(rva, orig, new, is_cave)] | LookupError}, gamestate-global rva), cached per exe file."""
    st = os.stat(exe_path)
    key = (exe_path.lower(), st.st_size, st.st_mtime_ns)
    if key not in _resolved:
        img = Image(exe_path)
        res = {}
        for n in ORDER:
            try:
                res[n] = PATCHES[n]['resolve'](img)
            except LookupError as e:
                res[n] = e
        try:
            g = img.find_one(GLOBAL_SIG, 'TrendManager gamestate')
            glob = g + 7 + struct.unpack('<i', img.bytes_at(g + 3, 4))[0]
        except LookupError:
            glob = None
        _resolved[key] = (res, glob)
    return _resolved[key]


# ================================================================ Win32 plumbing
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')
psapi = ctypes.WinDLL('psapi', use_last_error=True)
user32 = ctypes.WinDLL('user32', use_last_error=True)

PROCESS_NEEDED = 0x0010 | 0x0020 | 0x0008 | 0x0400 | 0x0800 | 0x1000   # VM_READ|VM_WRITE|VM_OPERATION|QUERY|SUSPEND_RESUME|QUERY_LIMITED
THREAD_GET_CONTEXT, THREAD_QUERY_INFORMATION = 0x0008, 0x0040
TH32CS_SNAPTHREAD, TH32CS_SNAPPROCESS, TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x4, 0x2, 0x8, 0x10
PAGE_EXECUTE_READWRITE = 0x40
CONTEXT_CONTROL = 0x00100001


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
                ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
                ('szExeFile', ctypes.c_wchar * 260)]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('th32ModuleID', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('GlblcntUsage', wt.DWORD), ('ProccntUsage', wt.DWORD), ('modBaseAddr', ctypes.c_void_p),
                ('modBaseSize', wt.DWORD), ('hModule', wt.HMODULE), ('szModule', ctypes.c_wchar * 256),
                ('szExePath', ctypes.c_wchar * 260)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ThreadID', wt.DWORD),
                ('th32OwnerProcessID', wt.DWORD), ('tpBasePri', ctypes.c_long), ('tpDeltaPri', ctypes.c_long),
                ('dwFlags', wt.DWORD)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD)] + \
               [(n, ctypes.c_size_t) for n in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                                               'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                                               'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage',
                                               'PrivateUsage')]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenThread.restype = wt.HANDLE
k32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
for _fn, _st in (('Process32FirstW', PROCESSENTRY32W), ('Process32NextW', PROCESSENTRY32W),
                 ('Module32FirstW', MODULEENTRY32W), ('Module32NextW', MODULEENTRY32W),
                 ('Thread32First', THREADENTRY32), ('Thread32Next', THREADENTRY32)):
    getattr(k32, _fn).argtypes = [wt.HANDLE, ctypes.POINTER(_st)]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k32.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.FlushInstructionCache.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
k32.GetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.CreateMutexW.restype = wt.HANDLE
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
ntdll.NtSuspendProcess.argtypes = [wt.HANDLE]
ntdll.NtResumeProcess.argtypes = [wt.HANDLE]
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]

INVALID_HANDLE = wt.HANDLE(-1).value


def find_pid(name=GAME_EXE):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE:
        return None
    try:
        e = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == name:
                return e.th32ProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return None


class Game:
    """An attached victoria3.exe process."""

    def __init__(self, pid):
        self.pid = pid
        self.h = k32.OpenProcess(PROCESS_NEEDED, False, pid)
        if not self.h:
            raise OSError(f'OpenProcess 失败（{ctypes.get_last_error()}）')
        try:
            self.base, self.size, self.path = self._module()
            self.patches, self.global_rva = resolve_all(self.path)
        except Exception:
            self.close()
            raise

    def _module(self):
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        if snap == INVALID_HANDLE:
            raise OSError(f'模块快照失败（{ctypes.get_last_error()}），游戏可能还在启动')
        try:
            e = MODULEENTRY32W(dwSize=ctypes.sizeof(MODULEENTRY32W))
            ok = k32.Module32FirstW(snap, ctypes.byref(e))
            while ok:
                if e.szModule.lower() == GAME_EXE:
                    return e.modBaseAddr, e.modBaseSize, e.szExePath
                ok = k32.Module32NextW(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        raise OSError('找不到 victoria3.exe 模块')

    def alive(self):
        code = wt.DWORD()
        return bool(self.h) and bool(k32.GetExitCodeProcess(self.h, ctypes.byref(code))) and code.value == 259

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def read(self, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)) or got.value != n:
            raise OSError(f'读取 {addr:#x} 失败（{ctypes.get_last_error()}）')
        return buf.raw

    def u64(self, addr):
        return int.from_bytes(self.read(addr, 8), 'little')

    def i32(self, addr):
        return int.from_bytes(self.read(addr, 4), 'little', signed=True)

    def _write_code(self, addr, data):
        old = wt.DWORD()
        if not k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise OSError(f'VirtualProtectEx 失败（{ctypes.get_last_error()}）')
        try:
            n = ctypes.c_size_t()
            if not k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), data, len(data), ctypes.byref(n)) or n.value != len(data):
                raise OSError(f'WriteProcessMemory 失败（{ctypes.get_last_error()}）')
        finally:
            k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), len(data), old.value, ctypes.byref(wt.DWORD()))
        k32.FlushInstructionCache(self.h, ctypes.c_void_p(addr), len(data))

    def ensure_extra(self, rva, size=EXTRA_SIZE):
        """Commit an executable block at base+rva (idempotent)."""
        addr = self.base + rva
        try:
            self.read(addr, 1)
            return
        except OSError:
            pass
        got = k32.VirtualAllocEx(self.h, ctypes.c_void_p(addr), size, 0x3000, PAGE_EXECUTE_READWRITE)
        if got != addr:
            raise OSError(f'VirtualAllocEx 在 {addr:#x} 失败（{ctypes.get_last_error()}），该地址已被占用')

    def _inv_addr(self):
        sites = self.patches.get('cap_investment')
        if isinstance(sites, Exception) or not sites:
            raise RuntimeError('cap_investment 不可用')
        return self.base + sites[0][0] + INV_DATA

    def invest_get(self):
        """{'budget','min','calls','throttled','max_p','max_n'}; counters stay 0 until the patch has been enabled."""
        raw = self.read(self._inv_addr(), 4 * len(INV_FIELDS))
        return dict(zip(INV_FIELDS, struct.unpack('<%dI' % len(INV_FIELDS), raw)))

    def invest_set(self, budget=None, minimum=None, reset_stats=False):
        """Change the cap parameters (and optionally zero the counters) while the game runs."""
        addr = self._inv_addr()
        cur = self.invest_get()
        vals = [budget if budget is not None else cur['budget'], minimum if minimum is not None else cur['min']]
        if reset_stats:
            vals += [0, 0, 0, 0]
        else:
            vals += [cur['calls'], cur['throttled'], cur['max_p'], cur['max_n']]
        self._write_code(addr, struct.pack('<6I', *vals))

    def thread_samples(self):
        """[(tid, rip, rsp)] of every thread of the (suspended) process; rip/rsp None when unreadable."""
        out = []
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        raw = ctypes.create_string_buffer(0x4D0 + 16)
        ctx = (ctypes.addressof(raw) + 15) & ~15
        try:
            e = THREADENTRY32(dwSize=ctypes.sizeof(THREADENTRY32))
            ok = k32.Thread32First(snap, ctypes.byref(e))
            while ok:
                if e.th32OwnerProcessID == self.pid:
                    th = k32.OpenThread(THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, False, e.th32ThreadID)
                    if th:
                        ctypes.memset(ctx, 0, 0x4D0)
                        ctypes.c_uint32.from_address(ctx + 0x30).value = CONTEXT_CONTROL
                        if k32.GetThreadContext(th, ctypes.c_void_p(ctx)):
                            out.append((e.th32ThreadID, ctypes.c_uint64.from_address(ctx + 0xF8).value,
                                        ctypes.c_uint64.from_address(ctx + 0x98).value))
                        k32.CloseHandle(th)
                ok = k32.Thread32Next(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return out

    def thread_rips(self):
        """RIP of every thread of the (suspended) process; None when unreadable."""
        rips = []
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        raw = ctypes.create_string_buffer(0x4D0 + 16)
        ctx = (ctypes.addressof(raw) + 15) & ~15
        try:
            e = THREADENTRY32(dwSize=ctypes.sizeof(THREADENTRY32))
            ok = k32.Thread32First(snap, ctypes.byref(e))
            while ok:
                if e.th32OwnerProcessID == self.pid:
                    th = k32.OpenThread(THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, False, e.th32ThreadID)
                    if th:
                        ctypes.memset(ctx, 0, 0x4D0)
                        ctypes.c_uint32.from_address(ctx + 0x30).value = CONTEXT_CONTROL
                        ok_ctx = k32.GetThreadContext(th, ctypes.c_void_p(ctx))
                        rips.append(ctypes.c_uint64.from_address(ctx + 0xF8).value if ok_ctx else None)
                        k32.CloseHandle(th)
                ok = k32.Thread32Next(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return rips

    def write_sites(self, writes, tries=50):
        """Write [(addr, bytes)] with the process suspended, only when no thread sits strictly inside a site."""
        for _ in range(tries):
            ntdll.NtSuspendProcess(self.h)
            try:
                rips = self.thread_rips()
                busy = any(r is None or any(a < r < a + len(b) for a, b in writes) for r in rips)
                if not busy:
                    for a, b in writes:
                        self._write_code(a, b)
                    return
            finally:
                ntdll.NtResumeProcess(self.h)
            time.sleep(0.02)
        raise RuntimeError('游戏线程一直停在补丁位置，已放弃写入（未做任何修改）')

    # --- telemetry -------------------------------------------------------
    def clock_hours(self):
        if self.global_rva is None:
            raise OSError('未定位到游戏时钟')
        p = self.u64(self.base + self.global_rva)
        gs = self.u64(p + 0x608)
        return self.i32(gs + 8)

    def memory(self):
        c = PROCESS_MEMORY_COUNTERS_EX(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX))
        psapi.GetProcessMemoryInfo(self.h, ctypes.byref(c), c.cb)
        return c.WorkingSetSize, c.PrivateUsage

    def cpu_seconds(self):
        t = [wt.FILETIME() for _ in range(4)]
        k32.GetProcessTimes(self.h, *[ctypes.byref(x) for x in t])
        f = lambda ft: (ft.dwHighDateTime << 32 | ft.dwLowDateTime) / 1e7
        return f(t[2]) + f(t[3])


# ================================================================ patch logic
def patch_state(game, name):
    """'on' / 'off' / 'mismatch' / 'unavailable'."""
    sites = game.patches[name]
    if isinstance(sites, Exception):
        return 'unavailable'
    states = set()
    for rva, orig, new, is_cave in sites:
        try:
            cur = game.read(game.base + rva, len(orig))
        except OSError:
            if is_cave == 2:                        # block not allocated yet
                continue
            raise
        if is_cave:
            if cur not in (orig, new):              # cave must hold padding or our own code
                return 'mismatch'
            continue
        states.add('on' if cur == new else 'off' if cur == orig else 'mismatch')
    return states.pop() if len(states) == 1 else 'mismatch'


def all_states(game):
    return {n: patch_state(game, n) for n in ORDER}


def set_patch(game, name, enable):
    st = patch_state(game, name)
    if st == 'unavailable':
        raise RuntimeError(f'{name}：{game.patches[name]}')
    if st == 'mismatch':
        raise RuntimeError(f'{name}：游戏代码与预期不符，拒绝修改（已测试版本 {TESTED_VERSION}）')
    if (st == 'on') == enable:
        return False
    sites = game.patches[name]
    if enable:      # caves first, then the jumps into them
        for r, o, n, cave in sites:
            if cave == 2:
                game.ensure_extra(r)
        if name == 'cap_investment' and game.invest_get()['budget'] == 0:      # fresh block: write the defaults
            game.invest_set(INVEST_BUDGET, INVEST_MIN)
        writes = [(game.base + r, n) for r, o, n, cave in sites if cave] + \
                 [(game.base + r, n) for r, o, n, cave in sites if not cave]
    else:           # restore the jumps; the cave stays behind as unreachable code
        writes = [(game.base + r, o) for r, o, n, cave in sites if not cave]
    game.write_sites(writes)
    if patch_state(game, name) != ('on' if enable else 'off'):
        raise RuntimeError(f'{name}：写入后校验失败')
    return True


def hours_to_date(h):
    """Game clock: hours since 1.1.1 of year -5000 (365-day years)."""
    days, _ = divmod(h, 24)
    year, doy = divmod(days, 365)
    months = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    m = 0
    while doy >= months[m]:
        doy -= months[m]
        m += 1
    return f'{year - 5000}.{m + 1}.{doy + 1}'


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            user = json.load(f)
        for n, v in user.get('enabled', {}).items():
            if n in PATCHES:                       # silently drops keys of older versions
                cfg['enabled'][n] = bool(v)
        for k in ('auto_apply', 'hotkey'):
            cfg[k] = user.get(k, cfg[k])
        global INVEST_BUDGET, INVEST_MIN
        INVEST_BUDGET = max(100, min(10**7, int(user.get('invest_budget', INVEST_BUDGET))))
        INVEST_MIN = max(1, min(64, int(user.get('invest_min', INVEST_MIN))))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ================================================================ hotspot sampler
# functions found with REA (1.14.5, default image base): address -> (length in bytes from REA, meaning).
# .pdata splits big functions into chunks, so the extents come from REA instead of the exception table.
PROFILE_ROOTS = {
    0x140f771a0: (1039, '自主投资·串行落地（每国循环 n 次）'),
    0x140f7ec40: (233, '自主投资·并行选下一个建筑'),
    0x140c86860: (286, '  选下一个建筑（单国）'),
    0x1415152a0: (3571, '  选建筑的决策函数'),
    0x140f5df00: (1211, '  落地一笔投资'),
    0x1414f8d00: (960, '  落地后刷新优先级 SetPotentialPrivateInvestmentPriorities'),
    0x1414fab60: (175, '    └ 全表扫描选最优'),
    0x1414fe4b0: (418, '    └ 对候选并行重算'),
    0x140c888a0: (1249, 'UpdateInvestmentCaches（事件触发）'),
}


def profile(game, seconds=30, interval_ms=15, out=print, top=22):
    """Read-only hotspot sampler: suspends the game for a few hundred microseconds per sample, records every thread's
    RIP and scans its stack for return addresses into the functions of PROFILE_ROOTS (inclusive attribution;
    stale stack slots can cause a few false hits)."""
    import bisect, collections
    img = Image(game.path)
    text_lo, text_hi = game.base + img.text_rva, game.base + img.text_rva + len(img.code)
    roots = {}
    for va, (length, label) in PROFILE_ROOTS.items():
        roots[(game.base + va - img.base, game.base + va - img.base + length)] = label
    selfc, incl = collections.Counter(), collections.Counter()
    busy = total = outside = iters = 0
    per = collections.defaultdict(lambda: [0, 0, collections.Counter(), collections.Counter()])   # tid -> samples, busy, self, labels
    end = time.time() + seconds
    out(f'采样 {seconds:.0f} 秒（每 {interval_ms} ms 一次，只读）……请保持游戏在卡顿的状态下运行')
    while time.time() < end:
        ntdll.NtSuspendProcess(game.h)
        try:
            samples = game.thread_samples()
            stacks = {}
            for tid, rip, rsp in samples:
                stacks[tid] = b''
                for n in (4096, 2048, 1024, 256):       # the stack may end before 4 KiB
                    try:
                        stacks[tid] = game.read(rsp, n)
                        break
                    except OSError:
                        continue
        finally:
            ntdll.NtResumeProcess(game.h)
        iters += 1
        for tid, rip, rsp in samples:
            total += 1
            hits = set()
            raw = stacks[tid]
            addrs = [rip] + list(struct.unpack('<%dQ' % (len(raw) // 8), raw[:len(raw) // 8 * 8]))
            for a in addrs:
                for (lo, hi), label in roots.items():
                    if lo <= a < hi:
                        hits.add(label)
            if text_lo <= rip < text_hi:
                busy += 1
                i = bisect.bisect_right(img.fstarts, rip - game.base) - 1
                fn = hex(game.base + img.funcs[i][0]) if i >= 0 else '?'
                selfc[fn] += 1
                per[tid][1] += 1
                per[tid][2][fn] += 1
            else:
                outside += 1
            per[tid][0] += 1
            for h in hits:
                incl[h] += 1
                per[tid][3][h] += 1
        time.sleep(interval_ms / 1000)
    n = max(total, 1)
    out(f'共 {total} 个线程样本；其中在游戏代码里 {busy}（{busy / n:.0%}），在系统/等待里 {outside}（{outside / n:.0%}）')
    out('—— 含调用者的占比（占全部线程样本；一次样本可能同时命中父子函数）——')
    for label, c in sorted(incl.items(), key=lambda kv: -kv[1]):
        out(f'  {c / n:6.1%}  {label}')
    if not incl:
        out('  （没有样本落在自主投资相关函数里）')
    out('—— 自身占比最高的函数（运行时地址）——')
    for a, c in selfc.most_common(top):
        out(f'  {c / n:6.1%}  {a}  (RVA {int(a, 16) - game.base:#x})')
    out(f'—— 最忙的线程（共 {iters} 轮采样；占比 = 该线程在游戏代码里的轮数 / 总轮数）——')
    for tid, (smp, bz, sc, lc) in sorted(per.items(), key=lambda kv: -kv[1][1])[:5]:
        out(f'  线程 {tid}: 在游戏代码里 {bz / max(iters, 1):.0%}')
        for a, c in sc.most_common(4):
            out(f'      自身 {c / max(iters, 1):5.1%}  {a} (RVA {int(a, 16) - game.base:#x})')
        for l, c in lc.most_common(3):
            out(f'      含调用 {c / max(iters, 1):5.1%}  {l.strip()}')
    return {'total': total, 'busy': busy, 'incl': dict(incl), 'self': dict(selfc)}


# ================================================================ benchmark
def bench(game, seconds, label='', out=print):
    d0, c0, t0 = game.clock_hours(), game.cpu_seconds(), time.perf_counter()
    peak_ws = peak_priv = 0
    while time.perf_counter() - t0 < seconds:
        time.sleep(0.5)
        ws, pv = game.memory()
        peak_ws, peak_priv = max(peak_ws, ws), max(peak_priv, pv)
    d1, c1, t1 = game.clock_hours(), game.cpu_seconds(), time.perf_counter()
    dt = t1 - t0
    days = (d1 - d0) / 24
    r = {'label': label, 'seconds': round(dt, 1), 'game_days': days, 'days_per_sec': days / dt,
         'cpu_cores': (c1 - c0) / dt, 'cpu_sec_per_game_day': (c1 - c0) / max(days, 1e-9),
         'working_set_mb': peak_ws / 2**20, 'private_mb': peak_priv / 2**20,
         'from': hours_to_date(d0), 'to': hours_to_date(d1)}
    if days < 1:
        out(f'[{label}] 游戏日期没有推进（暂停中？）本轮数据无效')
    out(f"[{label}] {r['from']} → {r['to']}  {days:.0f} 天 / {dt:.0f} 秒 = {r['days_per_sec']:.2f} 天/秒；"
        f"CPU {r['cpu_cores']:.2f} 核，{r['cpu_sec_per_game_day']:.3f} CPU秒/游戏日；"
        f"内存 {r['private_mb']:.0f} MB（工作集 {r['working_set_mb']:.0f} MB）")
    return r


# ================================================================ controller (daemon / GUI core)
class Controller:
    def __init__(self, out=print):
        self.cfg = load_config()
        self.game = None
        self.out = out
        self.lock = threading.RLock()
        self.cmds = queue.Queue()
        self.stop = threading.Event()
        self.log_pos = {}
        self.last_attach_error = None
        self.readonly = False

    def poll(self):
        with self.lock:
            if self.game and not self.game.alive():
                self.out('游戏已退出，等待下次启动……')
                self.game.close()
                self.game = None
            if self.game:
                return
            pid = find_pid()
            if not pid:
                self.last_attach_error = None
                return
            try:
                self.game = Game(pid)
            except (OSError, ValueError) as e:
                if str(e) != self.last_attach_error:       # report once, retry quietly
                    self.out(f'附加中：{e}')
                    self.last_attach_error = str(e)
                return
            self.last_attach_error = None
            st = all_states(self.game)
            self.readonly = 'mismatch' in st.values()
            self.out(f'已附加 victoria3.exe（PID {pid}）状态：' + '，'.join(f'{n}={s}' for n, s in st.items()))
            for n in ORDER:
                if isinstance(self.game.patches[n], Exception):
                    self.out(f'  {n} 不可用：{self.game.patches[n]}')
            if self.readonly:
                self.out(f'警告：代码与预期不符（已测试 {TESTED_VERSION}），进入只读模式，不做任何修改')
                return
            self.reset_log_positions()
            if self.cfg['auto_apply']:
                self.apply_config()

    def apply_config(self):
        with self.lock:
            if not self.game or self.readonly:
                return
            for n in ORDER:
                if isinstance(self.game.patches[n], Exception):
                    continue
                try:
                    if set_patch(self.game, n, self.cfg['enabled'][n]):
                        self.out(f"{'启用' if self.cfg['enabled'][n] else '关闭'}：{PATCHES[n]['title']}")
                except (RuntimeError, OSError) as e:
                    self.out(str(e))

    def set(self, name, enable):
        with self.lock:
            self.cfg['enabled'][name] = enable
            save_config(self.cfg)
            self.apply_config()

    def set_main(self, enable):
        """On/off for the default fixes; advanced options keep their own setting."""
        with self.lock:
            for n in ORDER:
                if PATCHES[n]['default']:
                    self.cfg['enabled'][n] = enable
            save_config(self.cfg)
            self.apply_config()

    def main_on(self):
        return all(self.cfg['enabled'][n] for n in ORDER if PATCHES[n]['default'])

    def states(self):
        with self.lock:
            return all_states(self.game) if self.game else None

    # in-game button / console: debug_log = "TRENDKEEPER:<CMD>" ends up in game.log
    def reset_log_positions(self):
        for fn in ('game.log', 'debug.log', 'error.log'):
            p = os.path.join(LOG_DIR, fn)
            try:
                self.log_pos[p] = os.path.getsize(p)
            except OSError:
                self.log_pos[p] = 0

    def scan_logs(self):
        for p, pos in list(self.log_pos.items()):
            try:
                size = os.path.getsize(p)
                if size < pos:
                    pos = 0                          # rotated
                if size == pos:
                    continue
                with open(p, 'rb') as f:
                    f.seek(pos)
                    chunk = f.read(size - pos)
                self.log_pos[p] = size
            except OSError:
                continue
            for m in re.finditer(re.escape(MARKER) + r'([A-Z_]+)', chunk.decode('utf-8', 'replace')):
                self.handle_marker(m.group(1))

    def handle_marker(self, cmd):
        self.out(f'收到游戏内指令：{cmd}')
        if cmd == 'ON':
            self.set_main(True)
        elif cmd == 'OFF':
            self.set_main(False)
        elif cmd == 'TOGGLE':
            self.set_main(not self.main_on())
        else:
            on, _, name = cmd.partition('_')
            if on in ('ON', 'OFF') and name.lower() in PATCHES:
                self.set(name.lower(), on == 'ON')

    def hotkey_loop(self):
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_F9, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x78, 0x0312
        if not user32.RegisterHotKey(None, 0x7E4B, MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, VK_F9):
            self.out('快捷键 Ctrl+Alt+F9 注册失败（可能被占用）')
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.cmds.put('toggle')

    def worker(self):
        while not self.stop.is_set():
            try:
                self.poll()
                if self.game:
                    self.scan_logs()
                while not self.cmds.empty():
                    if self.cmds.get_nowait() == 'toggle':
                        self.set_main(not self.main_on())
                        self.out('快捷键：' + ('修复已开启' if self.main_on() else '修复已关闭'))
            except Exception as e:                   # keep the helper alive whatever happens
                self.out(f'错误：{e}')
            self.stop.wait(1.0)

    def start(self):
        threading.Thread(target=self.worker, daemon=True).start()
        if self.cfg.get('hotkey', True):
            threading.Thread(target=self.hotkey_loop, daemon=True).start()


def single_instance():
    h = k32.CreateMutexW(None, False, 'Local\\TrendKeeperVictoria3')
    return ctypes.get_last_error() != 183, h          # 183 = ERROR_ALREADY_EXISTS


# ================================================================ GUI
def run_gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox

    first, _mutex = single_instance()
    if not first:
        tk.Tk().withdraw()
        messagebox.showinfo('Trend Keeper', 'Trend Keeper 已经在运行。')
        return

    root = tk.Tk()
    root.title('Trend Keeper · 维多利亚3 历史曲线修复')
    root.geometry('660x600')
    msgs = queue.Queue()
    ctl = Controller(out=lambda s: msgs.put(time.strftime('%H:%M:%S ') + s))

    status = tk.StringVar(value='等待游戏启动……')
    ttk.Label(root, textvariable=status, font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', padx=12, pady=(10, 4))

    vars_, state_lbl = {}, {}
    box = ttk.LabelFrame(root, text='补丁（勾选即时生效，游戏运行中可随时开关）')
    box.pack(fill='x', padx=12, pady=4)
    for n in ORDER:
        row = ttk.Frame(box)
        row.pack(fill='x', padx=6, pady=(6, 0))
        v = tk.BooleanVar(value=ctl.cfg['enabled'][n])
        vars_[n] = v
        ttk.Checkbutton(row, text=PATCHES[n]['title'], variable=v,
                        command=lambda n=n: threading.Thread(target=ctl.set, args=(n, vars_[n].get()), daemon=True).start()
                        ).pack(side='left')
        state_lbl[n] = ttk.Label(row, text='', width=12)
        state_lbl[n].pack(side='right')
        ttk.Label(box, text=PATCHES[n]['desc'], wraplength=600,
                  foreground='#555' if PATCHES[n]['default'] else '#a05000').pack(anchor='w', padx=28)

    inv = ttk.Frame(root)
    inv.pack(fill='x', padx=12, pady=2)
    ttk.Label(inv, text='自主投资限制：预算').pack(side='left')
    inv_budget = tk.StringVar(value=str(INVEST_BUDGET))
    inv_min = tk.StringVar(value=str(INVEST_MIN))
    ttk.Entry(inv, textvariable=inv_budget, width=8).pack(side='left', padx=3)
    ttk.Label(inv, text='最小').pack(side='left')
    ttk.Entry(inv, textvariable=inv_min, width=4).pack(side='left', padx=3)

    def inv_apply(reset=False):
        def run():
            with ctl.lock:
                g = ctl.game
            if not g or ctl.readonly:
                ctl.out('游戏未运行或处于只读模式')
                return
            try:
                if reset and not inv_budget.get().strip():
                    g.invest_set(reset_stats=True)
                else:
                    g.invest_set(int(inv_budget.get()), int(inv_min.get()), reset_stats=True)
                ctl.out('已更新自主投资限制参数并清零统计（无需重启游戏）')
            except (ValueError, OSError, RuntimeError) as e:
                ctl.out(f'设置失败：{e}')
        threading.Thread(target=run, daemon=True).start()
    ttk.Button(inv, text='应用并清零统计', command=inv_apply).pack(side='left', padx=4)
    inv_lbl = ttk.Label(inv, text='', foreground='#555')
    inv_lbl.pack(side='left', padx=6)

    opts = ttk.Frame(root)
    opts.pack(fill='x', padx=12, pady=6)
    auto = tk.BooleanVar(value=ctl.cfg['auto_apply'])

    def set_auto():
        ctl.cfg['auto_apply'] = auto.get()
        save_config(ctl.cfg)
    ttk.Checkbutton(opts, text='游戏启动后自动应用', variable=auto, command=set_auto).pack(side='left')
    ttk.Button(opts, text='修复全开', command=lambda: threading.Thread(target=ctl.set_main, args=(True,), daemon=True).start()).pack(side='left', padx=6)
    ttk.Button(opts, text='修复全关', command=lambda: threading.Thread(target=ctl.set_main, args=(False,), daemon=True).start()).pack(side='left')

    def do_bench():
        def run():
            with ctl.lock:
                g = ctl.game
            if not g:
                ctl.out('游戏未运行')
                return
            ctl.out('性能测试 60 秒：请保持同一游戏速度、不要暂停……')
            try:
                bench(g, 60, '当前设置', out=ctl.out)
            except OSError as e:
                ctl.out(f'测试失败：{e}')
        threading.Thread(target=run, daemon=True).start()
    ttk.Button(opts, text='性能测试 60s', command=do_bench).pack(side='right')

    def do_profile():
        def run():
            with ctl.lock:
                g = ctl.game
            if not g:
                ctl.out('游戏未运行')
                return
            try:
                profile(g, 30, 15, out=ctl.out)
            except OSError as e:
                ctl.out(f'采样失败：{e}')
        threading.Thread(target=run, daemon=True).start()
    ttk.Button(opts, text='热点采样 30s', command=do_profile).pack(side='right', padx=6)

    ttk.Label(root, text='快捷键 Ctrl+Alt+F9 开/关修复　|　游戏内：CMF 侧边栏按钮，或控制台 effect tk_toggle = yes',
              foreground='#555').pack(anchor='w', padx=12)
    log = scrolledtext.ScrolledText(root, height=14, font=('Consolas', 9))
    log.pack(fill='both', expand=True, padx=12, pady=8)
    labels = {'on': '● 已生效', 'off': '○ 未生效', 'mismatch': '× 版本不符', 'unavailable': '× 不可用'}

    def refresh():
        while not msgs.empty():
            log.insert('end', msgs.get() + '\n')
            log.see('end')
        try:
            st = ctl.states()
        except OSError:
            st = None
        if st:
            try:
                date = hours_to_date(ctl.game.clock_hours())
            except OSError:
                date = '?'
            status.set(f'已附加 victoria3.exe（PID {ctl.game.pid}）　游戏日期 {date}' + ('　【只读】' if ctl.readonly else ''))
            for n in ORDER:
                state_lbl[n].config(text=labels[st[n]])
                vars_[n].set(ctl.cfg['enabled'][n])
            try:
                if st.get('cap_investment') == 'on':
                    v = ctl.game.invest_get()
                    inv_lbl.config(text=f"国家次数 {v['calls']}　被限制 {v['throttled']}　最大潜在建筑 {v['max_p']}　原版最大落地 {v['max_n']}")
                else:
                    inv_lbl.config(text='（补丁未开启）')
            except (OSError, RuntimeError):
                inv_lbl.config(text='')
        else:
            status.set('等待游戏启动……（启动后自动附加）')
            for n in ORDER:
                state_lbl[n].config(text='')
        root.after(700, refresh)

    ctl.start()
    refresh()
    root.mainloop()


# ================================================================ CLI
def selftest(exe):
    print(f'exe: {exe}')
    res, glob = resolve_all(exe)
    img = Image(exe)
    ok = True
    for n in ORDER:
        sites = res[n]
        if isinstance(sites, Exception):
            print(f'  {n:14s} 失败：{sites}')
            ok = False
            continue
        print(f'  {n:14s} OK')
        for rva, orig, new, cave in sites:
            good = True if cave == 2 else img.bytes_at(rva, len(orig)) == orig
            ok &= good
            print(f'      {"cave" if cave else "site"} {img.base + rva:#x}  {orig.hex(" ")} -> {new.hex(" ")}  '
                  f'{"原始字节一致" if good else "原始字节不符!"}')
    print(f'  游戏时钟全局指针 {img.base + glob:#x}' if glob else '  游戏时钟全局指针：未找到')
    ok = ok and glob is not None
    print('自检通过' if ok else '自检未通过')
    return 0 if ok else 1


DAEMON_LOG = os.path.join(HERE, 'trend_keeper.log')
STARTUP_LNK = os.path.join(os.environ.get('APPDATA', ''), r'Microsoft\Windows\Start Menu\Programs\Startup',
                           'Trend Keeper (Victoria 3).lnk')


def daemon_log(msg):
    """Daemon output (pythonw has no console): append to tools/trend_keeper.log, keep it under ~1 MB."""
    try:
        if os.path.exists(DAEMON_LOG) and os.path.getsize(DAEMON_LOG) > 1 << 20:
            os.replace(DAEMON_LOG, DAEMON_LOG + '.1')
        with open(DAEMON_LOG, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')
    except OSError:
        pass


def autostart(cmd):
    """Start the headless daemon at Windows logon (a shortcut in the user's Startup folder)."""
    import subprocess
    if cmd == 'autostart-status':
        print('已安装' if os.path.exists(STARTUP_LNK) else '未安装', STARTUP_LNK)
        return 0
    if cmd == 'uninstall-autostart':
        if os.path.exists(STARTUP_LNK):
            os.remove(STARTUP_LNK)
        print('已移除开机自启')
        return 0
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{STARTUP_LNK}');"
          f"$s.TargetPath='{pythonw}';"
          f"$s.Arguments='-I -B \"{os.path.abspath(__file__)}\" daemon';"
          f"$s.WorkingDirectory='{HERE}';$s.WindowStyle=7;"
          f"$s.Description='Trend Keeper: keeps Victoria 3 history graphs complete';$s.Save()")
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], check=True)
    print('已安装开机自启：', STARTUP_LNK)
    return 0


def main(argv):
    for s in (sys.stdout, sys.stderr):
        if s and hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8', errors='replace')
    cmd = argv[0] if argv else 'gui'
    if cmd == 'gui':
        return run_gui()
    if cmd == 'selftest':
        return selftest(argv[1] if len(argv) > 1 else DEFAULT_EXE)
    if cmd == 'daemon':
        first, _m = single_instance()
        if not first:
            print('Trend Keeper 已经在运行')
            return 1
        ctl = Controller(out=daemon_log)
        daemon_log(f'后台守护启动（PID {os.getpid()}），等待 victoria3.exe ……')
        ctl.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    if cmd in ('install-autostart', 'uninstall-autostart', 'autostart-status'):
        return autostart(cmd)
    pid = find_pid()
    if not pid:
        print('游戏未运行')
        return 1
    g = Game(pid)
    try:
        if cmd == 'status':
            print(f'PID {pid}  base {g.base:#x}  {g.path}')
            try:
                print('游戏日期', hours_to_date(g.clock_hours()))
            except OSError as e:
                print('读取日期失败', e)
            for n, s in all_states(g).items():
                print(f'  {n:14s} {s:11s} {PATCHES[n]["title"]}')
            ws, pv = g.memory()
            print(f'内存：私有 {pv / 2**20:.0f} MB，工作集 {ws / 2**20:.0f} MB')
        elif cmd in ('on', 'off'):
            for n in argv[1:] or [n for n in ORDER if PATCHES[n]['default']]:
                print(n, '已修改' if set_patch(g, n, cmd == 'on') else '无需修改', '→', patch_state(g, n))
        elif cmd == 'invest':
            if argv[1:2] == ['set']:
                g.invest_set(int(argv[2]), int(argv[3]) if len(argv) > 3 else None, reset_stats=True)
            elif argv[1:2] == ['reset']:
                g.invest_set(reset_stats=True)
            st = g.invest_get()
            print(f"cap_investment {patch_state(g, 'cap_investment')}  预算 {st['budget']}  最小 {st['min']}")
            print(f"  统计（自上次重置）：国家次数 {st['calls']}，被限制 {st['throttled']}，"
                  f"见过的最大潜在建筑数 {st['max_p']}，原版最大落地次数 {st['max_n']}")
        elif cmd == 'profile':
            profile(g, float(argv[1]) if len(argv) > 1 else 30, float(argv[2]) if len(argv) > 2 else 15)
        elif cmd == 'bench':
            bench(g, float(argv[1]) if len(argv) > 1 else 60, 'bench')
        elif cmd == 'compare':
            secs = float(argv[1]) if len(argv) > 1 else 60
            names = argv[2:] or ['fix_pop_stats']
            before = {n: patch_state(g, n) == 'on' for n in names}
            res = []
            for label, en in (('A 关', False), ('B 开', True), ('A 关', False)):
                for n in names:
                    set_patch(g, n, en)
                res.append(bench(g, secs, label))
            for n, en in before.items():
                set_patch(g, n, en)
            base = (res[0]['cpu_sec_per_game_day'] + res[2]['cpu_sec_per_game_day']) / 2
            speed = (res[0]['days_per_sec'] + res[2]['days_per_sec']) / 2
            noise = abs(res[0]['cpu_sec_per_game_day'] - res[2]['cpu_sec_per_game_day']) / base
            print(f"结论（{', '.join(names)}）：开启后 CPU秒/游戏日 {res[1]['cpu_sec_per_game_day'] / base - 1:+.1%}，"
                  f"游戏速度 {res[1]['days_per_sec'] / speed - 1:+.1%}；两次关闭之间自身波动 ±{noise / 2:.1%}")
        else:
            print(__doc__)
    finally:
        g.close()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
