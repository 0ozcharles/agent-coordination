import unittest

from agent_coordination import stats


def row(cell, i, p1, p2):
    return {"cell": cell, "instance": i, "payoff_p1": p1, "payoff_p2": p2, "joint": p1 + p2}


class StatsTest(unittest.TestCase):
    def test_sign_and_wilcoxon(self):
        self.assertEqual(stats.sign_test([1, 2, 3, -1, 0])["p"], 0.625)
        self.assertEqual(stats.sign_test([])["p"], 1.0)
        # eight positive differences, untied: both exact tests give 2 / 2^8
        d = [1, 2, 3, 4, 5, 6, 7, 8]
        self.assertAlmostEqual(stats.wilcoxon(d)["p"], 2 / 256)
        self.assertAlmostEqual(stats.sign_test(d)["p"], 2 / 256)
        # a symmetric sample is not significant; zeros are dropped
        self.assertGreater(stats.wilcoxon([1, -1, 2, -2, 0, 3, -3])["p"], 0.9)
        # with ties the normal approximation is used and stays in [0, 1]
        w = stats.wilcoxon([1, 1, 1, 2, 2, -1, 3, 3, 3, 3] * 3)
        self.assertTrue(0 < w["p"] < 0.01)

    def test_bootstrap_and_summary(self):
        d = [5.0] * 10 + [7.0] * 10
        lo, hi = stats.bootstrap_ci(d, resamples=2000)
        self.assertTrue(5.0 <= lo < 6.0 < hi <= 7.0)
        s = stats.summary(d, resamples=500)
        self.assertEqual((s["n"], s["mean"], s["wins"], s["losses"]), (20, 6.0, 20, 0))
        self.assertIsNone(stats.summary([]))

    def test_holm(self):
        adj = stats.holm([0.01, 0.04, 0.03])
        self.assertEqual([round(a, 2) for a in adj], [0.03, 0.06, 0.06])
        self.assertEqual(stats.holm([0.5, 0.9]), [1.0, 1.0])

    def test_seat_averaged_differences(self):
        # two instances; the strategy adopted in seat 1 and in seat 2, against the same neutral pair
        R = {}
        for i, (nn, a, b) in enumerate([((60, 62), (70, 61), (63, 68)), ((50, 50), (55, 40), (45, 58))]):
            R[("neutral-neutral", i)] = row("neutral-neutral", i, *nn)
            R[("collaborating-neutral", i)] = row("collaborating-neutral", i, *a)
            R[("neutral-collaborating", i)] = row("neutral-collaborating", i, *b)
            R[("collaborating-collaborating", i)] = row("collaborating-collaborating", i, nn[0] + 4, nn[1] + 4)
        al = stats.alone(R, "collaborating")
        # instance 0: adopter gets 70 (seat 1) and 68 (seat 2), mean 69, against the neutral agent's mean 61: +8
        self.assertEqual(al["own"], [8.0, 6.5])
        # partner: 61 and 63 (mean 62) against 61: +1; instance 1: 40 and 45 against 50: -7.5
        self.assertEqual(al["partner"], [1.0, -7.5])
        self.assertEqual(stats.pair(R, "collaborating"), [8.0, 8.0])
        # percent of the seat's maximum when the seats have different scales
        scaled = stats.alone(R, "collaborating", scale={"p1": 100.0, "p2": 50.0})
        self.assertEqual(scaled["own"][0], (70 + 136) / 2 - (60 + 124) / 2)
        # a strategy played in one seat only is compared with the neutral agent in that seat, not with both seats
        one = {k: v for k, v in R.items() if k[0] != "neutral-collaborating"}
        self.assertEqual(stats.alone(one, "collaborating")["own"], [10.0, 5.0])
        # a reply to a third strategy and a contrast between two strategies are matched by seat as well
        R[("competing-neutral", 0)], R[("neutral-competing", 0)] = row("competing-neutral", 0, 66, 50), row("neutral-competing", 0, 52, 64)
        self.assertEqual(stats.contrast(R, "collaborating", "competing")[0], ((70 - 66) + (68 - 64)) / 2)


if __name__ == "__main__":
    unittest.main()
