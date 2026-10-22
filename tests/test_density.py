import unittest
from density import group_totals, dot_positions, plural, render_density

REFERENCE = [163,162,151,149,145,138,137,137,135,133,133,132,130,129,126,125,121,118,107,100,88]

class DensityTests(unittest.TestCase):
    def test_reference_groups(self):
        groups=group_totals(REFERENCE)
        self.assertEqual([(g['count'],g['low'],g['high'],g['spread']) for g in groups],
                         [(5,88,121,33),(11,125,138,13),(3,145,151,6),(2,162,163,1)])
        self.assertEqual(sorted(v for g in groups for v in g['totals']),sorted(REFERENCE))
        self.assertEqual(groups[1]['max_gap'],3)
        self.assertEqual(group_totals(REFERENCE[::-1]),groups)

    def test_positions_preserve_exact_points_and_ties(self):
        coords=dot_positions(REFERENCE,80,170)
        for value,(x,_) in zip(sorted(REFERENCE),coords):
            self.assertEqual(x,100+(value-80)/90*1400)
        for i,(a,b) in enumerate(zip(sorted(REFERENCE),sorted(REFERENCE)[1:])):
            if a==b:
                self.assertEqual(coords[i][0],coords[i+1][0])
                self.assertNotEqual(coords[i][1],coords[i+1][1])

    def test_edge_cases(self):
        self.assertEqual(group_totals([]),[])
        for values in ([0],[-10],[50]*30,[1,2],[1,2,3],[0,100,101,102,300]):
            groups=group_totals(values)
            self.assertEqual(sorted(v for g in groups for v in g['totals']),sorted(values))
            self.assertLessEqual(len(groups),4)
            self.assertTrue(render_density(values,'2026-10-20 07:00').getvalue().startswith(b'\x89PNG'))
        with self.assertRaises(ValueError):
            render_density([],'now')

    def test_russian_plurals(self):
        self.assertEqual([plural(n,('команда','команды','команд')) for n in (1,2,5,11,21,22,25,101,111)],
                         ['команда','команды','команд','команд','команда','команды','команд','команда','команд'])
