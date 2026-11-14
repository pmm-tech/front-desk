# -*- coding: utf-8 -*-
# Copyright (c) 2026, Core Initiative and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import unittest

from inn.patches.v1_2.fix_unsynced_room_booking_segments import (
    plan_unsynced_segment_repair,
    propose_changes,
    same_value,
)


def reservation(status, arrival, departure, room, name="RSV-26854"):
    return {
        "name": name,
        "status": status,
        "arrival": arrival,
        "departure": departure,
        "actual_room_id": room,
    }


def segment(name, room, start, end, status, res="RSV-26854"):
    return {
        "name": name,
        "reference_name": res,
        "room_id": room,
        "start": start,
        "end": end,
        "status": status,
    }


def fields(changes):
    return sorted((c["name"], c["field"], str(c["old_value"]), str(c["new_value"])) for c in changes)


class TestPlanUnsyncedSegmentRepair(unittest.TestCase):
    """The planner mirrors sync_booking_segments_with_reservation, so these fix
    the repair to the same rules the live code uses."""

    def test_in_house_single_booked_segment_is_planned_as_stayed(self):
        """The reported drift: a checked-in guest whose segment never synced.

        RSV-26854's shape. Only the status is wrong -- the dates and the room
        already match -- so exactly one change is proposed.
        """
        res = reservation("In House", "2026-11-13 20:01:55", "2026-11-15 12:00:00", "R-405")
        segs = [segment("eepideskn4", "R-405", "2026-11-13", "2026-11-15", "Booked")]

        changes = plan_unsynced_segment_repair(res, segs)

        self.assertEqual(fields(changes), [("eepideskn4", "status", "Booked", "Stayed")])
        self.assertEqual(changes[0]["reason"], "in_house_single_segment")
        self.assertEqual(changes[0]["reservation_id"], "RSV-26854")

    def test_in_house_single_segment_already_in_sync_plans_nothing(self):
        res = reservation("In House", "2026-11-13 20:01:55", "2026-11-15 12:00:00", "R-405")
        segs = [segment("BK-1", "R-405", "2026-11-13", "2026-11-15", "Stayed")]

        self.assertEqual(plan_unsynced_segment_repair(res, segs), [])

    def test_date_only_difference_is_not_a_change(self):
        """A reservation arrival carries a time; the segment date does not."""
        res = reservation("In House", "2026-11-13 20:01:55", "2026-11-15 12:00:00", "R-405")
        segs = [segment("BK-1", "R-405", "2026-11-13", "2026-11-15", "Stayed")]

        self.assertEqual(plan_unsynced_segment_repair(res, segs), [])

    def test_in_house_changed_departure_moves_the_last_segment_only(self):
        res = reservation("In House", "2026-11-13 20:01:55", "2026-11-16", "R-601")
        segs = [
            segment("BK-OLD", "R-405", "2026-11-13", "2026-11-14", "Finished"),
            segment("BK-NEW", "R-601", "2026-11-14", "2026-11-15", "Stayed"),
        ]

        changes = plan_unsynced_segment_repair(res, segs)

        self.assertEqual(fields(changes), [("BK-NEW", "end", "2026-11-15", "2026-11-16")])
        self.assertEqual(changes[0]["reason"], "in_house_departure_on_last_segment")

    def test_multi_segment_move_boundaries_are_never_touched(self):
        """A move's internal boundary must survive the repair.

        Only the last non-canceled segment takes the departure, the room and
        the 'Stayed' status, exactly as the live sync does. The first segment
        would only take the arrival, and here it already has it.
        """
        res = reservation("In House", "2026-11-13 20:01:55", "2026-11-15 12:00:00", "R-601")
        segs = [
            segment("BK-OLD", "R-405", "2026-11-13", "2026-11-14", "Booked"),
            segment("BK-NEW", "R-601", "2026-11-14", "2026-11-15", "Booked"),
        ]

        changes = plan_unsynced_segment_repair(res, segs)

        touched = {(c["name"], c["field"]) for c in changes}
        self.assertNotIn(("BK-OLD", "end"), touched, "the old segment's end is the move boundary")
        self.assertNotIn(("BK-NEW", "start"), touched, "the new segment's start is the move boundary")
        self.assertNotIn(("BK-OLD", "status"), touched, "only the last segment takes the status")
        self.assertEqual(fields(changes), [("BK-NEW", "status", "Booked", "Stayed")])

    def test_canceled_segment_is_not_the_active_one(self):
        """A same-day move leaves a Canceled segment; the live one is next to it."""
        res = reservation("In House", "2026-11-13 20:01:55", "2026-11-15 12:00:00", "R-405")
        segs = [
            segment("BK-CANCELED", "R-405", "2026-11-13", "2026-11-13", "Canceled"),
            segment("BK-LIVE", "R-405", "2026-11-13", "2026-11-15", "Booked"),
        ]

        changes = plan_unsynced_segment_repair(res, segs)

        self.assertEqual(fields(changes), [("BK-LIVE", "status", "Booked", "Stayed")])

    def test_finish_reservation_finishes_the_last_segment(self):
        res = reservation("Finish", "2026-11-13 20:01:55", "2026-11-15 12:00:00", "R-405")
        segs = [segment("BK-1", "R-405", "2026-11-13", "2026-11-15", "Stayed")]

        changes = plan_unsynced_segment_repair(res, segs)

        self.assertEqual(fields(changes), [("BK-1", "status", "Stayed", "Finished")])
        self.assertEqual(changes[0]["reason"], "finish_last_segment")

    def test_finish_reservation_leaves_earlier_segments_alone(self):
        res = reservation("Finish", "2026-11-13 20:01:55", "2026-11-15 12:00:00", "R-601")
        segs = [
            segment("BK-OLD", "R-405", "2026-11-13", "2026-11-14", "Finished"),
            segment("BK-NEW", "R-601", "2026-11-14", "2026-11-15", "Booked"),
        ]

        changes = plan_unsynced_segment_repair(res, segs)

        self.assertEqual(fields(changes), [("BK-NEW", "status", "Booked", "Finished")])

    def test_reserved_reservation_is_out_of_scope(self):
        """A booking that has not checked in is legitimately 'Booked'."""
        res = reservation("Reserved", "2026-12-01", "2026-12-03", "R-405")
        segs = [segment("BK-1", "R-405", "2026-12-01", "2026-12-03", "Booked")]

        self.assertEqual(plan_unsynced_segment_repair(res, segs), [])

    def test_canceled_reservation_is_out_of_scope(self):
        res = reservation("Cancel", "2026-11-13", "2026-11-15", "R-405")
        segs = [segment("BK-1", "R-405", "2026-11-13", "2026-11-15", "Booked")]

        self.assertEqual(plan_unsynced_segment_repair(res, segs), [])

    def test_all_canceled_segments_need_a_human_not_a_bulk_repair(self):
        """Re-activating a canceled segment is a judgement call, not a repair."""
        res = reservation("In House", "2026-11-13", "2026-11-15", "R-405")
        segs = [segment("BK-1", "R-405", "2026-11-13", "2026-11-15", "Canceled")]

        self.assertEqual(plan_unsynced_segment_repair(res, segs), [])

    def test_missing_values_are_never_proposed(self):
        """A reservation with no arrival cannot be repaired into a value."""
        res = reservation("In House", None, None, None)
        segs = [segment("BK-1", "R-405", "2026-11-13", "2026-11-15", "Booked")]

        changes = plan_unsynced_segment_repair(res, segs)

        self.assertEqual(fields(changes), [("BK-1", "status", "Booked", "Stayed")])

    def test_plan_is_idempotent(self):
        """Applying the plan must make a second plan empty."""
        res = reservation("In House", "2026-11-13 20:01:55", "2026-11-16 12:00:00", "R-601")
        segs = [
            segment("BK-OLD", "R-405", "2026-11-13", "2026-11-13", "Canceled"),
            segment("BK-MID", "R-405", "2026-11-13", "2026-11-14", "Booked"),
            segment("BK-LIVE", "R-601", "2026-11-14", "2026-11-15", "Booked"),
        ]

        changes = plan_unsynced_segment_repair(res, segs)
        self.assertTrue(changes, "fixture should need repair")

        applied = [dict(s) for s in segs]
        by_name = {s["name"]: s for s in applied}
        for change in changes:
            by_name[change["name"]][change["field"]] = change["new_value"]

        self.assertEqual(plan_unsynced_segment_repair(res, applied), [])


class TestProposeChanges(unittest.TestCase):
    def test_same_value_for_dates_ignores_time(self):
        self.assertTrue(same_value("start", "2026-11-13", "2026-11-13 20:01:55"))

    def test_same_value_for_text_is_exact(self):
        self.assertTrue(same_value("status", "Stayed", "Stayed"))
        self.assertFalse(same_value("status", "Booked", "Stayed"))

    def test_none_old_value_is_compared_as_empty_text(self):
        self.assertTrue(same_value("room_id", None, ""))
        self.assertFalse(same_value("room_id", None, "R-405"))

    def test_propose_changes_skips_missing_wanted_values(self):
        changes = propose_changes(segment("BK-1", "R-405", "2026-11-13", "2026-11-15", "Booked"),
                                  {"room_id": None, "status": "Stayed"}, "test")
        self.assertEqual(fields(changes), [("BK-1", "status", "Booked", "Stayed")])


if __name__ == "__main__":
    unittest.main()
