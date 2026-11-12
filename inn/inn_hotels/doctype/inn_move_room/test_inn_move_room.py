# -*- coding: utf-8 -*-
# Copyright (c) 2020, Core Initiative and Contributors
# See license.txt
from __future__ import unicode_literals

import datetime
import unittest

import frappe
from frappe.utils import getdate

from inn.inn_hotels.doctype.inn_move_room.inn_move_room import (
	calculate_room_move_booking_boundaries,
)


class TestInnMoveRoom(unittest.TestCase):
	def test_mid_stay_move_truncates_old_and_starts_new(self):
		"""Mid-stay move (T_move > T_arr): old booking ends at T_move (Finished),

		new booking starts at T_move and ends at original departure (Stayed).
		"""
		old_start = getdate("2026-10-28")
		old_end = getdate("2026-11-02")
		move_date = getdate("2026-10-31")

		boundaries = calculate_room_move_booking_boundaries(old_start, old_end, move_date)

		self.assertEqual(boundaries["old_status"], "Finished")
		self.assertEqual(boundaries["old_end"], getdate("2026-10-31"))
		self.assertEqual(boundaries["new_start"], getdate("2026-10-31"))
		self.assertEqual(boundaries["new_end"], getdate("2026-11-02"))
		self.assertEqual(boundaries["new_status"], "Stayed")

	def test_same_day_move_cancels_old_booking(self):
		"""Same-day move (T_move == T_arr): guest spent 0 nights in old room.

		Old booking status must be 'Canceled' so it generates 0 room nights.
		"""
		old_start = getdate("2026-10-31")
		old_end = getdate("2026-11-03")
		move_date = getdate("2026-10-31")

		boundaries = calculate_room_move_booking_boundaries(old_start, old_end, move_date)

		self.assertEqual(boundaries["old_status"], "Canceled")
		self.assertEqual(boundaries["old_end"], getdate("2026-10-31"))
		self.assertEqual(boundaries["new_start"], getdate("2026-10-31"))
		self.assertEqual(boundaries["new_end"], getdate("2026-11-03"))
		self.assertEqual(boundaries["new_status"], "Stayed")

	def test_move_date_before_arrival_raises(self):
		"""Move date cannot be earlier than booking start date."""
		old_start = getdate("2026-10-31")
		old_end = getdate("2026-11-03")
		move_date = getdate("2026-10-30")

		with self.assertRaises(frappe.ValidationError):
			calculate_room_move_booking_boundaries(old_start, old_end, move_date)

	def test_move_date_at_or_after_departure_raises(self):
		"""Move date cannot be at or after scheduled departure date."""
		old_start = getdate("2026-10-31")
		old_end = getdate("2026-11-03")
		move_date = getdate("2026-11-03")

		with self.assertRaises(frappe.ValidationError):
			calculate_room_move_booking_boundaries(old_start, old_end, move_date)

