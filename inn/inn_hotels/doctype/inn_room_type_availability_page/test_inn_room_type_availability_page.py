# -*- coding: utf-8 -*-
# Copyright (c) 2021, Core Initiative and Contributors
# See license.txt
"""Tests for the room type availability page's availability matrix.

These cover the bed-type split and the date boundaries that the report relies
on, so they are deliberately DB-free: rooms, totals and bookings are passed in
rather than read from the site. That keeps the arithmetic testable without
creating Inn Room Booking records, which would also move real occupancy.
"""
from __future__ import unicode_literals
import unittest
from datetime import date, timedelta
import frappe
from inn.inn_hotels.doctype.inn_room_type_availability_page.inn_room_type_availability_page import (
	UNSPECIFIED,
	build_availability_rows,
	build_window_dates,
)

D1 = date(2026, 3, 24)
D2 = date(2026, 3, 25)
D3 = date(2026, 3, 26)


def window(*days):
	return list(days)


def total(room_type, bed_type, count):
	return frappe._dict(room_type=room_type, bed_type=bed_type, total=count)


def booking(room_type, bed_type, start, end):
	return frappe._dict(room_type=room_type, bed_type=bed_type, start=start, end=end)


def rows_by_key(rows):
	return {(row["room_type"], row["bed_type"]): row for row in rows}


class TestInnRoomTypeAvailabilityPage(unittest.TestCase):
	def test_splits_one_room_type_into_bed_type_rows(self):
		rows = build_availability_rows(
			["Superior"],
			[total("Superior", "Twin", 3), total("Superior", "Queen", 2)],
			[
				booking("Superior", "Twin", D1, D2),
				booking("Superior", "Queen", D1, D3),
			],
			window(D1, D2, D3),
		)

		by_key = rows_by_key(rows)
		self.assertEqual(set(by_key), {("Superior", "Twin"), ("Superior", "Queen")})
		self.assertEqual(by_key[("Superior", "Twin")]["total"], 3)
		self.assertEqual(
			by_key[("Superior", "Twin")]["available"],
			{"2026-03-24": 2, "2026-03-25": 3, "2026-03-26": 3},
		)
		self.assertEqual(by_key[("Superior", "Queen")]["total"], 2)
		self.assertEqual(
			by_key[("Superior", "Queen")]["available"],
			{"2026-03-24": 1, "2026-03-25": 1, "2026-03-26": 2},
		)

	def test_blank_bed_type_folds_into_unspecified_and_sorts_last(self):
		rows = build_availability_rows(
			["Superior"],
			[total("Superior", "Twin", 1), total("Superior", None, 2), total("Superior", "", 1)],
			[],
			window(D1),
		)

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0]["bed_type"], "Twin")
		self.assertEqual(rows[1]["bed_type"], UNSPECIFIED)
		self.assertEqual(rows[1]["total"], 3)
		self.assertEqual(rows[1]["available"], {"2026-03-24": 3})

	def test_booking_end_date_is_exclusive(self):
		"""A guest checking out on a date leaves that date sellable."""
		rows = build_availability_rows(
			["Superior"],
			[total("Superior", "Twin", 1)],
			[
				booking("Superior", "Twin", D1, D3),
				booking("Superior", "Twin", D2, D3 + timedelta(days=1)),
			],
			window(D1, D2, D3),
		)

		self.assertEqual(
			rows[0]["available"],
			{"2026-03-24": 0, "2026-03-25": -1, "2026-03-26": 0},
		)

	def test_bookings_outside_the_window_are_ignored(self):
		rows = build_availability_rows(
			["Superior"],
			[total("Superior", "Twin", 2)],
			[
				booking("Superior", "Twin", D1 - timedelta(days=5), D2),
				booking("Superior", "Twin", D3 + timedelta(days=2), D3 + timedelta(days=3)),
			],
			window(D1, D2, D3),
		)

		self.assertEqual(
			rows[0]["available"],
			{"2026-03-24": 1, "2026-03-25": 2, "2026-03-26": 2},
		)

	def test_overbooking_keeps_negative_availability(self):
		rows = build_availability_rows(
			["Superior"],
			[total("Superior", "Twin", 1)],
			[
				booking("Superior", "Twin", D1, D2),
				booking("Superior", "Twin", D1, D2),
			],
			window(D1),
		)

		self.assertEqual(rows[0]["available"]["2026-03-24"], -1)

	def test_room_type_without_rooms_gets_one_zero_row(self):
		rows = build_availability_rows(["Empty"], [], [], window(D1, D2))

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["room_type"], "Empty")
		self.assertEqual(rows[0]["bed_type"], UNSPECIFIED)
		self.assertEqual(rows[0]["total"], 0)
		self.assertEqual(rows[0]["available"], {"2026-03-24": 0, "2026-03-25": 0})

	def test_every_row_carries_every_requested_date(self):
		dates = window(D1, D2, D3)
		rows = build_availability_rows(
			["Superior", "Deluxe"],
			[total("Superior", "Twin", 1)],
			[booking("Superior", "Twin", D1, D2)],
			dates,
		)

		expected_keys = {day.isoformat() for day in dates}
		self.assertEqual(len(rows), 2)
		for row in rows:
			self.assertEqual(set(row["available"]), expected_keys)

	def test_window_dates_include_both_ends(self):
		dates = build_window_dates(D1, D3)

		self.assertEqual(dates, [D1, D2, D3])
		self.assertEqual(build_window_dates(D1, D1), [D1])

	def test_ordering_is_room_type_then_bed_type_with_blank_last(self):
		rows = build_availability_rows(
			["Superior", "Deluxe"],
			[
				total("Superior", "King", 1),
				total("Deluxe", "Twin", 1),
				total("Deluxe", None, 1),
			],
			[],
			window(D1),
		)

		self.assertEqual(
			[(row["room_type"], row["bed_type"]) for row in rows],
			[("Deluxe", "Twin"), ("Deluxe", UNSPECIFIED), ("Superior", "King")],
		)

	def test_booking_for_an_unknown_room_type_still_surfaces(self):
		"""Orphan bookings must not be silently dropped from the totals."""
		rows = build_availability_rows(
			["Superior"],
			[total("Superior", "Twin", 1)],
			[booking("Ghost", "Twin", D1, D2)],
			window(D1),
		)

		by_key = rows_by_key(rows)
		self.assertIn(("Ghost", "Twin"), by_key)
		self.assertEqual(by_key[("Ghost", "Twin")]["total"], 0)
		self.assertEqual(by_key[("Ghost", "Twin")]["available"], {"2026-03-24": -1})
