"""Offline tests for Trend Keeper (no game needed).

    python -m unittest discover -s trend_keeper/tests -v

The patch-resolver tests need victoria3.exe (set $VIC3_EXE or install it through Steam); they are skipped otherwise.
The tests that disassemble generated code need `capstone` (pip install capstone) and are skipped without it.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'tools'))
import trend_keeper as tk                                       # noqa: E402

try:
    import capstone
except ImportError:
    capstone = None

EXE = tk.find_exe()
HAS_EXE = os.path.isfile(EXE) and os.path.getsize(EXE) == 97774200       # the tested 1.14.5 build
D = 24                                                                  # hours per game day
BASE = (1900 + 5000) * 365 * D                                           # 1900.1.1 on the game clock


class GameClock(unittest.TestCase):
    def test_hours_to_date(self):
        self.assertEqual(tk.hours_to_date(BASE), '1900.1.1')
        self.assertEqual(tk.hours_to_date(BASE + 31 * D), '1900.2.1')
        self.assertEqual(tk.hours_to_date(BASE + 364 * D), '1900.12.31')
        self.assertEqual(tk.hours_to_date(BASE + 365 * D), '1901.1.1')            # no leap years

    def test_span_text(self):
        self.assertIn('10 天', tk.span_text(BASE, BASE + 10 * D))
        self.assertIn('8 个月', tk.span_text(BASE, BASE + 250 * D))
        self.assertIn('2 年', tk.span_text(BASE, BASE + 800 * D))
        self.assertIn('→', tk.span_text(BASE, BASE + D))


class CoverageTracking(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'coverage.json')
        self.on = {n: 'on' for n in tk.COVERAGE_PATCHES}
        self.off = {n: 'off' for n in tk.COVERAGE_PATCHES}

    def run_days(self, cov, a, b, states):
        for day in range(a, b):
            cov.tick(BASE + day * D, states)

    def test_continuous_range(self):
        cov = tk.Coverage(self.path)
        self.run_days(cov, 0, 40, self.on)
        self.assertEqual(cov.stretches('keep_history'), [(BASE, BASE + 39 * D)])
        self.assertEqual(len(cov.segments), 1)

    def test_patch_off_creates_a_hole_only_beyond_the_gap(self):
        cov = tk.Coverage(self.path)
        self.run_days(cov, 0, 10, self.on)
        self.run_days(cov, 10, 18, self.off)            # 8 days off: merged
        self.run_days(cov, 18, 30, self.on)
        self.assertEqual(len(cov.stretches('keep_history')), 1)
        self.run_days(cov, 30, 60, self.off)            # 30 days off: a real hole
        self.run_days(cov, 60, 70, self.on)
        self.assertEqual(len(cov.stretches('keep_history')), 2)

    def test_clock_jump_starts_a_new_save_and_returning_adopts_the_old_one(self):
        cov = tk.Coverage(self.path)
        self.run_days(cov, 0, 30, self.on)
        cov.tick(BASE + 900 * D, self.on)               # another save was loaded
        self.assertEqual(len(cov.segments), 2)
        self.assertEqual(cov.current(), (BASE + 900 * D, BASE + 900 * D))
        cov.tick(BASE + 20 * D, self.on)                # back inside the first one
        self.assertEqual(len(cov.segments), 2)
        self.assertEqual(cov.current()[0], BASE)

    def test_persistence_round_trip(self):
        cov = tk.Coverage(self.path)
        self.run_days(cov, 0, 30, self.on)
        cov.detach()
        again = tk.Coverage(self.path)
        self.assertEqual(len(again.segments), 1)
        again.tick(BASE + 31 * D, self.on)               # helper restarted inside the same save
        self.assertEqual(len(again.segments), 1)
        self.assertEqual(again.current()[0], BASE)

    def test_corrupt_file_is_ignored(self):
        with open(self.path, 'w') as f:
            f.write('{not json')
        self.assertEqual(tk.Coverage(self.path).segments, [])

    def test_only_twenty_segments_are_kept(self):
        cov = tk.Coverage(self.path)
        for i in range(30):
            cov.tick(BASE + i * 1000 * D, self.on)
        self.assertLessEqual(len(cov.segments), 20)


class Config(unittest.TestCase):
    def test_every_patch_has_what_the_gui_needs(self):
        for n in tk.ORDER:
            p = tk.PATCHES[n]
            for key in ('title', 'desc', 'resolve', 'default'):
                self.assertIn(key, p, n)
        self.assertEqual(set(tk.ORDER), set(tk.PATCHES))
        self.assertEqual(set(tk.DEFAULT_CONFIG['enabled']), set(tk.ORDER))

    def test_risky_patches_are_off_by_default(self):
        self.assertFalse(tk.PATCHES['cap_investment']['default'])      # changes the simulation
        self.assertFalse(tk.PATCHES['track_pops']['default'])          # memory grows without bound

    def test_coverage_patches_exist(self):
        for n in tk.COVERAGE_PATCHES:
            self.assertIn(n, tk.PATCHES)


@unittest.skipUnless(HAS_EXE, 'victoria3.exe 1.14.5 not found (set $VIC3_EXE)')
class PatchResolvers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = tk.Image(EXE)

    def test_every_patch_resolves_and_original_bytes_match(self):
        for n in tk.ORDER:
            sites = tk.PATCHES[n]['resolve'](self.img)
            self.assertTrue(sites, n)
            for rva, orig, new, cave in sites:
                self.assertEqual(len(orig), len(new), n)
                if cave == 2:                                   # helper-allocated block: not in the file
                    continue
                self.assertEqual(self.img.bytes_at(rva, len(orig)), orig, f'{n} {rva:#x}')

    def test_code_caves_never_overlap(self):
        spans = []
        for n in tk.ORDER:
            for rva, orig, new, cave in tk.PATCHES[n]['resolve'](tk.Image(EXE)):
                if cave:
                    spans.append((rva, rva + len(new), n))
        spans.sort()
        for (a0, a1, an), (b0, b1, bn) in zip(spans, spans[1:]):
            self.assertLessEqual(a1, b0, f'{an} overlaps {bn}')

    def test_track_all_states_only_touches_the_conditional_jump(self):
        (rva, orig, new, cave), = tk.resolve_track_all_states(self.img)
        self.assertEqual((orig[0], new), (0x75, b'\x90\x90'))
        self.assertFalse(cave)

    def test_patch_sites_do_not_overlap(self):
        spans = []
        for n in tk.ORDER:
            for rva, orig, new, cave in tk.PATCHES[n]['resolve'](tk.Image(EXE)):
                if not cave:
                    spans.append((rva, rva + len(orig), n))
        spans.sort()
        for (a0, a1, an), (b0, b1, bn) in zip(spans, spans[1:]):
            if an != bn:
                self.assertLessEqual(a1, b0, f'{an} overlaps {bn}')


@unittest.skipUnless(HAS_EXE and capstone, 'needs victoria3.exe 1.14.5 and capstone')
class GeneratedCode(unittest.TestCase):
    def test_cap_investment_code_is_what_we_think(self):
        img = tk.Image(EXE)
        (cave, _o, code, kind), (site, _so, jmp, _c) = tk.resolve_cap_investment(img)
        self.assertEqual(kind, 2)
        self.assertLess(len(code), tk.INV_DATA)                           # code never reaches the data area
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        ins = list(md.disasm(code, 0x140000000 + cave))
        self.assertEqual(sum(len(i.bytes) for i in ins), len(code))       # decodes cleanly to the last byte
        self.assertEqual(ins[-1].mnemonic, 'jmp')
        self.assertEqual(int(ins[-1].op_str, 16), 0x140000000 + site + 5)  # back to `test ebx,ebx`
        data = range(0x140000000 + cave + tk.INV_DATA, 0x140000000 + cave + tk.INV_DATA + 4 * len(tk.INV_FIELDS))
        for i in ins:                                                      # every rip-relative operand hits the data area
            if 'rip' in i.op_str:
                disp = int(i.op_str.split('rip')[1].strip(' +]').split(']')[0], 16)
                self.assertIn(i.address + len(i.bytes) + disp, data)
        # the hook replaces exactly the two instructions `cmp edx,ebx ; cmovg ebx,edx`
        self.assertEqual(img.bytes_at(site, 5), bytes.fromhex('3bd30f4fda'))
        self.assertEqual(jmp[0], 0xE9)


if __name__ == '__main__':
    unittest.main(verbosity=2)
