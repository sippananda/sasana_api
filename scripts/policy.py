"""School closure rule. Pure logic, no network, no files.

The rule (from the school's "Haze Protocol Update"):
  - Each day, take the 2:00 pm API of four DOE stations and average them.
  - School CLOSES the next day if the average is above 180 on any single day,
    or above 150 for three days in a row.
  - While closed, school REOPENS the next day when the average is 150 or below.
  - The count of three days restarts after school reopens.

Choices this code makes where the notice is silent (see README, "Open questions"):
  - Every calendar day counts for the rule, weekends included. So the Sunday
    reading decides Monday. The web page shows weekends as "no school".
  - The average is rounded to a whole number before it is compared with the
    limits. 0.5 and above goes up: 149.5 becomes 150, 141.4 becomes 141.
  - A day with no usable reading changes nothing: status and count stay as they were.
"""

import math

OPEN = "open"
CLOSED = "closed"


def round_half_up(x):
    """Whole number. 0.5 and above goes up: 149.5 -> 150, 141.4 -> 141."""
    if x is None:
        return None
    return int(math.floor(x + 0.5))


def average(values, min_stations):
    """Mean of the readings that exist, rounded to a whole number.

    None if too few stations reported.
    """
    present = [v for v in values if v is not None]
    if len(present) < min_stations or not present:
        return None
    return round_half_up(sum(present) / len(present))


def evaluate(days, rule):
    """Apply the rule to a list of days in date order.

    days: [{"date": "YYYY-MM-DD", "average": number or None}, ...]
          The list must have one entry per calendar day, with no gaps.
          An average with decimals is rounded to a whole number first.
    rule: the "rule" block of config.json.

    Returns a new list. Each entry has the input fields plus:
      school_status      "open" / "closed" on that day (None before start_date)
      decision_next_day  "open" / "closed" for the following day
      streak             days in a row above the limit, after this day's reading
      reason             short code
      reason_text        one plain sentence
    """
    hi = rule["close_above_single_day"]
    lim = rule["close_above_streak"]
    need = rule["streak_days"]
    reopen = rule["reopen_at_or_below"]
    start = rule.get("start_date") or ""

    status = OPEN
    streak = 0
    out = []
    for day in days:
        row = dict(day)
        avg = round_half_up(day.get("average"))
        row["average"] = avg

        if day["date"] < start:
            row.update(school_status=None, decision_next_day=None, streak=None,
                       reason="before_start",
                       reason_text="Before the monitoring start date. Not counted.")
            out.append(row)
            continue

        row["school_status"] = status

        if avg is None:
            nxt = status
            reason = "no_data"
            text = ("No usable 2:00 pm reading. Status and count do not change. "
                    "Check the DOE website.")
        elif status == OPEN:
            streak = streak + 1 if avg > lim else 0
            if avg > hi:
                nxt, reason = CLOSED, "above_single_day"
                text = f"Average is above {hi}. School closes the next day."
            elif streak >= need:
                nxt, reason = CLOSED, "streak"
                text = (f"Average is above {lim} for {need} days in a row. "
                        "School closes the next day.")
            elif avg > lim:
                nxt, reason = OPEN, "counting"
                text = f"Average is above {lim}. Day {streak} of {need}. School stays open."
            else:
                nxt, reason = OPEN, "below"
                text = f"Average is {lim} or below. School stays open."
        else:  # closed
            if avg <= reopen:
                nxt, reason = OPEN, "reopen"
                text = f"Average is {reopen} or below. School reopens the next day."
            else:
                nxt, reason = CLOSED, "stays_closed"
                text = f"Average is still above {reopen}. School stays closed."

        # The count only runs while school is open. It restarts after reopening.
        shown_streak = streak if status == OPEN else 0
        if status == CLOSED or nxt == CLOSED:
            streak = 0

        row.update(decision_next_day=nxt, streak=shown_streak,
                   reason=reason, reason_text=text)
        out.append(row)
        status = nxt
    return out
