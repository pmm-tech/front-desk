# -*- coding: utf-8 -*-
# Copyright (c) 2026, Core Initiative and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import unittest
from frappe.utils import getdate
from inn.patches.v1_2.fix_overlapping_room_move_bookings import plan_room_booking_repair


class TestFixOverlappingRoomMoveBookings(unittest.TestCase):
    def test_mid_stay_move_truncates_old_booking_end(self):
        """Old room booking end must be truncated to move_date on mid-stay move."""
        res_id = "RSV-29690"
        moves = [
            {
                "name": "MV-001",
                "reservation_id": res_id,
                "old_room_id": "R-1012",
                "new_room": "R-1014",
                "arrival": "2026-10-31",
                "departure": "2026-11-02",
                "creation": "2026-10-31 14:00:00",
            }
        ]
        bookings_by_res = {
            res_id: [
                {
                    "name": "BK-001",
                    "reference_name": res_id,
                    "room_id": "R-1012",
                    "start": "2026-10-28",
                    "end": "2026-11-02",  # Corrupted end date!
                    "room_availability": "Room Sold",
                    "status": "Finished",
                },
                {
                    "name": "BK-002",
                    "reference_name": res_id,
                    "room_id": "R-1014",
                    "start": "2026-10-31",
                    "end": "2026-11-02",
                    "room_availability": "Room Sold",
                    "status": "Stayed",
                },
            ]
        }

        changes = plan_room_booking_repair(bookings_by_res, moves)

        # Expected: BK-001 end date updated from 2026-11-02 to 2026-10-31
        end_changes = [c for c in changes if c["name"] == "BK-001" and c["field"] == "end"]
        self.assertEqual(len(end_changes), 1)
        self.assertEqual(end_changes[0]["new_value"], "2026-10-31")
        self.assertEqual(end_changes[0]["reason"], "mid_stay_truncation")

    def test_same_day_move_cancels_old_booking(self):
        """Old room booking on same-day move (start == move_date) must be canceled."""
        res_id = "RSV-29700"
        moves = [
            {
                "name": "MV-002",
                "reservation_id": res_id,
                "old_room_id": "R-1001",
                "new_room": "R-1002",
                "arrival": "2026-10-31",
                "departure": "2026-11-02",
                "creation": "2026-10-31 15:00:00",
            }
        ]
        bookings_by_res = {
            res_id: [
                {
                    "name": "BK-SAME-OLD",
                    "reference_name": res_id,
                    "room_id": "R-1001",
                    "start": "2026-10-31",
                    "end": "2026-11-02",
                    "room_availability": "Room Sold",
                    "status": "Finished",
                },
                {
                    "name": "BK-SAME-NEW",
                    "reference_name": res_id,
                    "room_id": "R-1002",
                    "start": "2026-10-31",
                    "end": "2026-11-02",
                    "room_availability": "Room Sold",
                    "status": "Stayed",
                },
            ]
        }

        changes = plan_room_booking_repair(bookings_by_res, moves)

        status_changes = [c for c in changes if c["name"] == "BK-SAME-OLD" and c["field"] == "status"]
        self.assertEqual(len(status_changes), 1)
        self.assertEqual(status_changes[0]["new_value"], "Canceled")
        self.assertEqual(status_changes[0]["reason"], "same_day_cancellation")

    def test_corrupted_new_booking_start_date_restored(self):
        """New booking start date backdated to arrival must be restored to move_date."""
        res_id = "RSV-29705"
        moves = [
            {
                "name": "MV-003",
                "reservation_id": res_id,
                "old_room_id": "R-1005",
                "new_room": "R-1006",
                "arrival": "2026-10-31",
                "departure": "2026-11-03",
                "creation": "2026-10-31 16:00:00",
            }
        ]
        bookings_by_res = {
            res_id: [
                {
                    "name": "BK-OLD",
                    "reference_name": res_id,
                    "room_id": "R-1005",
                    "start": "2026-10-29",
                    "end": "2026-10-31",
                    "room_availability": "Room Sold",
                    "status": "Finished",
                },
                {
                    "name": "BK-NEW",
                    "reference_name": res_id,
                    "room_id": "R-1006",
                    "start": "2026-10-29",  # Corrupted start date!
                    "end": "2026-11-03",
                    "room_availability": "Room Sold",
                    "status": "Stayed",
                },
            ]
        }

        changes = plan_room_booking_repair(bookings_by_res, moves)

        start_changes = [c for c in changes if c["name"] == "BK-NEW" and c["field"] == "start"]
        self.assertEqual(len(start_changes), 1)
        self.assertEqual(start_changes[0]["new_value"], "2026-10-31")
        self.assertEqual(start_changes[0]["reason"], "start_date_restoration")

    def test_secondary_sweep_truncates_overlapping_segments(self):
        """Multi-segment reservation without move record has overlap resolved in pass 2."""
        res_id = "RSV-99999"
        moves = []
        bookings_by_res = {
            res_id: [
                {
                    "name": "BK-SWEEP-1",
                    "reference_name": res_id,
                    "room_id": "R-201",
                    "start": "2026-10-28",
                    "end": "2026-11-02",  # Overlaps with BK-SWEEP-2
                    "room_availability": "Room Sold",
                    "status": "Finished",
                    "creation": "2026-10-28 10:00:00",
                },
                {
                    "name": "BK-SWEEP-2",
                    "reference_name": res_id,
                    "room_id": "R-202",
                    "start": "2026-10-31",
                    "end": "2026-11-02",
                    "room_availability": "Room Sold",
                    "status": "Stayed",
                    "creation": "2026-10-31 10:00:00",
                },
            ]
        }

        changes = plan_room_booking_repair(bookings_by_res, moves)

        end_changes = [c for c in changes if c["name"] == "BK-SWEEP-1" and c["field"] == "end"]
        self.assertEqual(len(end_changes), 1)
        self.assertEqual(end_changes[0]["new_value"], "2026-10-31")
