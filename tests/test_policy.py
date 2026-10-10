"""Tests for the closure rule. Run:  python3 -m unittest discover -s tests"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import policy  # noqa: E402

RULE = {"close_from_single_day": 180, "close_from_streak": 150, "streak_days": 3,
        "reopen_below": 150, "min_stations": 3, "start_date": "2026-01-01"}


def run(averages, rule=RULE, first=date(2026, 3, 1)):
    days = [{"date": (first + timedelta(days=i)).isoformat(), "average": a}
            for i, a in enumerate(averages)]
    return policy.evaluate(days, rule)


def col(rows, key):
    return [r[key] for r in rows]


class Rule(unittest.TestCase):
    def test_clean_air_stays_open(self):
        rows = run([60, 99, 149])
        self.assertEqual(col(rows, "decision_next_day"), ["open"] * 3)
        self.assertEqual(col(rows, "streak"), [0, 0, 0])

    def test_single_day_at_180_or_above_closes_next_day(self):
        rows = run([120, 181])
        self.assertEqual(col(rows, "school_status"), ["open", "open"])
        self.assertEqual(rows[1]["decision_next_day"], "closed")
        self.assertEqual(rows[1]["reason"], "above_single_day")

    def test_exactly_180_closes(self):
        self.assertEqual(run([180])[0]["decision_next_day"], "closed")
        self.assertEqual(run([180])[0]["reason"], "above_single_day")
        self.assertEqual(run([179])[0]["decision_next_day"], "open")

    def test_exactly_150_counts_as_high(self):
        rows = run([151, 151, 149, 150])
        self.assertEqual(col(rows, "streak"), [1, 2, 0, 1])
        self.assertEqual(col(rows, "decision_next_day"), ["open"] * 4)
        rows = run([150, 150, 150])
        self.assertEqual(col(rows, "streak"), [1, 2, 3])
        self.assertEqual(rows[2]["decision_next_day"], "closed")
        self.assertEqual(rows[2]["reason"], "streak")
        # 149.5 is rounded to 150, so it counts too
        self.assertEqual(run([149.5])[0]["streak"], 1)
        self.assertEqual(run([149.4])[0]["streak"], 0)

    def test_three_days_above_150_closes_on_day_four(self):
        rows = run([151, 160, 170, 155])
        self.assertEqual(col(rows, "decision_next_day"),
                         ["open", "open", "closed", "closed"])
        self.assertEqual(col(rows, "school_status"),
                         ["open", "open", "open", "closed"])
        self.assertEqual(rows[2]["reason"], "streak")
        self.assertEqual(rows[3]["reason"], "stays_closed")

    def test_two_days_then_a_break_restarts_the_count(self):
        rows = run([160, 160, 140, 160, 160])
        self.assertEqual(col(rows, "streak"), [1, 2, 0, 1, 2])
        self.assertTrue(all(d == "open" for d in col(rows, "decision_next_day")))

    def test_reopens_only_below_150(self):
        rows = run([185, 160, 150, 149, 100])
        self.assertEqual(col(rows, "school_status"),
                         ["open", "closed", "closed", "closed", "open"])
        self.assertEqual(col(rows, "decision_next_day"),
                         ["closed", "closed", "closed", "open", "open"])
        self.assertEqual(rows[2]["reason"], "stays_closed")
        self.assertEqual(rows[3]["reason"], "reopen")

    def test_count_restarts_after_reopening(self):
        # closed by 3 days, reopen, then 2 more days above 150 must NOT close
        rows = run([160, 160, 160, 140, 160, 160, 160])
        self.assertEqual(col(rows, "decision_next_day"),
                         ["open", "open", "closed", "open", "open", "open", "closed"])
        self.assertEqual(col(rows, "streak"), [1, 2, 3, 0, 1, 2, 3])

    def test_days_above_150_while_closed_are_not_carried_over(self):
        # closed by 181; two more high days while closed; reopen; one high day -> count 1
        rows = run([181, 170, 170, 120, 170])
        self.assertEqual(rows[4]["streak"], 1)
        self.assertEqual(rows[4]["decision_next_day"], "open")

    def test_average_is_rounded_before_the_limits(self):
        # 0.5 and above goes up. Below 0.5 goes down.
        self.assertEqual(run([179.25])[0]["decision_next_day"], "open")    # 179
        self.assertEqual(run([179.5])[0]["decision_next_day"], "closed")   # 180
        self.assertEqual(run([149.25])[0]["streak"], 0)                    # 149
        self.assertEqual(run([149.5])[0]["streak"], 1)                     # 150
        self.assertEqual(run([149.5])[0]["average"], 150)
        # closed school: 149.25 is 149, so it reopens. 149.5 is 150, so it stays closed.
        self.assertEqual(run([185, 149.25])[1]["decision_next_day"], "open")
        self.assertEqual(run([185, 149.5])[1]["decision_next_day"], "closed")

    def test_missing_day_changes_nothing(self):
        rows = run([160, None, 160, 160])
        self.assertEqual(col(rows, "streak"), [1, 1, 2, 3])
        self.assertEqual(rows[3]["decision_next_day"], "closed")
        rows = run([181, None, 100])
        self.assertEqual(col(rows, "school_status"), ["open", "closed", "closed"])
        self.assertEqual(rows[2]["decision_next_day"], "open")

    def test_days_before_start_date_are_not_counted(self):
        rule = dict(RULE, start_date="2026-03-04")
        rows = run([170, 170, 170, 140], rule)
        self.assertEqual(col(rows, "reason")[:3], ["before_start"] * 3)
        self.assertEqual(rows[3]["school_status"], "open")
        self.assertEqual(rows[3]["decision_next_day"], "open")

    def test_real_week_3_to_10_oct_2026(self):
        # 2 pm averages of the four stations, from DOE, 3-10 Oct 2026
        real = [71.75, 85.75, 84.5, 132.25, 172.0, 161.0, 167.75, 146.5]
        rows = run(real, first=date(2026, 10, 3))
        self.assertEqual(col(rows, "streak"), [0, 0, 0, 0, 1, 2, 3, 0])
        self.assertEqual(rows[6]["decision_next_day"], "closed")   # 9 Oct -> closed 10 Oct
        self.assertEqual(rows[7]["school_status"], "closed")       # 10 Oct
        self.assertEqual(rows[7]["decision_next_day"], "open")     # reopens 11 Oct


class Average(unittest.TestCase):
    def test_four_stations(self):
        self.assertEqual(policy.average([165, 157, 162, 102], 3), 147)     # 146.5

    def test_rounding(self):
        self.assertEqual(policy.round_half_up(149.5), 150)
        self.assertEqual(policy.round_half_up(141.4), 141)
        self.assertEqual(policy.round_half_up(150.5), 151)   # not 150 (no "round to even")
        self.assertEqual(policy.round_half_up(149.49), 149)
        self.assertEqual(policy.round_half_up(160), 160)
        self.assertIsNone(policy.round_half_up(None))
        self.assertEqual(policy.average([149, 150, 150, 149], 3), 150)     # 149.5
        self.assertEqual(policy.average([142, 141, 141, 142], 3), 142)     # 141.5
        self.assertEqual(policy.average([141, 141, 142, None], 3), 141)    # 141.33

    def test_three_of_four(self):
        self.assertEqual(policy.average([150, None, 160, 170], 3), 160)

    def test_too_few(self):
        self.assertIsNone(policy.average([150, None, None, 170], 3))
        self.assertIsNone(policy.average([None] * 4, 3))


if __name__ == "__main__":
    unittest.main()
