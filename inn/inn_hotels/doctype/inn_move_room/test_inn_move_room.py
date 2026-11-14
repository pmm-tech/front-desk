# -*- coding: utf-8 -*-
# Copyright (c) 2020, Core Initiative and Contributors
# See license.txt
from __future__ import unicode_literals

import datetime
import unittest

import frappe
from frappe.utils import add_days, getdate, now

from inn.inn_hotels.doctype.inn_move_room.inn_move_room import (
	calculate_room_move_booking_boundaries,
	create_move_room_by_reservation,
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


class TestCreateMoveRoomByReservation(unittest.TestCase):
	"""The move must find the guest's live segment in the old room, whatever
	state the check-in sync left it in.

	RSV-26854 (Jakarta, 2026-11-14) is the case these pin: the guest was checked
	in but the sync never ran, so the only R-405 segment was status 'Booked'. The
	move looked for 'Stayed', found nothing, and failed; the staff created a Room
	Booking by hand and the move then worked.

	These are real DB tests: the lookup is what was broken, so a mock cannot
	reach it. The fixture spans today because the move hardcodes
	datetime.date.today() as the move date -- note that is the container clock,
	not frappe's today(), which applies the site timezone. They can disagree, so
	the fixture is built from the same value the function reads.
	"""

	RSV = "_T5-RSV-001"
	OLD_ROOM = "_T5-R405"
	NEW_ROOM = "_T5-R601"
	ROOM_TYPE = "_T5-RT-Deluxe"
	BED_TYPE = "_T5-BT-King"
	ROOM_RATE = "_T5-RATE"
	CHANNEL = "_T5-CH-Direct"
	CUSTOMER = "_T5-CUST-001"

	# ---------------------------------------------------------------- fixture
	def setUp(self):
		self.move_date = datetime.date.today()
		self.start = add_days(self.move_date, -1)
		self.end = add_days(self.move_date, 2)
		self._clean()
		self._seed()

	def tearDown(self):
		self._clean()

	def _clean(self):
		for table, key in (
			("tabInn Move Room", self.RSV),
			("tabInn Reservation", self.RSV),
			("tabInn Room", self.OLD_ROOM),
			("tabInn Room", self.NEW_ROOM),
			("tabInn Room Rate", self.ROOM_RATE),
			("tabInn Bed Type", self.BED_TYPE),
			("tabInn Room Type", self.ROOM_TYPE),
			("tabInn Channel", self.CHANNEL),
			("tabInn Customer", self.CUSTOMER),
		):
			column = "reservation_id" if table == "tabInn Move Room" else "name"
			frappe.db.sql("delete from `{0}` where {1} = %s".format(table, column), key)
		frappe.db.sql("delete from `tabInn Room Booking` where reference_name = %s", self.RSV)
		frappe.db.commit()

	def _seed(self):
		for table, name in (
			("tabInn Room Type", self.ROOM_TYPE),
			("tabInn Bed Type", self.BED_TYPE),
			("tabInn Room Rate", self.ROOM_RATE),
			("tabInn Customer", self.CUSTOMER),
		):
			frappe.db.sql(
				"""insert into `{0}`
				   (name, creation, modified, modified_by, owner, docstatus, idx)
				   values (%s, now(), now(), 'Administrator', 'Administrator', 0, 0)""".format(table),
				name,
			)
		frappe.db.sql(
			"""insert into `tabInn Channel`
			   (name, creation, modified, modified_by, owner, docstatus, idx,
			    channel_name, profit_sharing, sharing_type)
			   values (%s, now(), now(), 'Administrator', 'Administrator', 0, 0,
			           %s, 0, 'Percentage')""",
			(self.CHANNEL, self.CHANNEL),
		)
		for room in (self.OLD_ROOM, self.NEW_ROOM):
			frappe.db.sql(
				"""insert into `tabInn Room`
				   (name, creation, modified, modified_by, owner, docstatus, idx,
				    number, room_type, bed_type, floor, room_status)
				   values (%s, now(), now(), 'Administrator', 'Administrator', 0, 0,
				           %s, %s, %s, '4', 'Occupied Clean')""",
				(room, room, self.ROOM_TYPE, self.BED_TYPE),
			)
		frappe.db.sql(
			"""insert into `tabInn Reservation`
			   (name, creation, modified, modified_by, owner, docstatus, idx,
			    naming_series, status, customer_id, type, channel,
			    expected_arrival, expected_departure, arrival, departure,
			    room_type, bed_type, room_id, actual_room_id, room_rate,
			    actual_room_rate, init_actual_room_rate)
			   values (%s, now(), now(), 'Administrator', 'Administrator', 0, 0,
			           'RSV-', 'In House', %s, 'INDIVIDUAL', %s,
			           %s, %s, %s, %s,
			           %s, %s, %s, %s, %s, 500000, 500000)""",
			(self.RSV, self.CUSTOMER, self.CHANNEL, self.start, self.end,
			 self.start, self.end, self.ROOM_TYPE, self.BED_TYPE,
			 self.OLD_ROOM, self.OLD_ROOM, self.ROOM_RATE),
		)
		frappe.db.commit()

	def _add_segment(self, name, room, status, start=None, end=None, creation=None):
		frappe.db.sql(
			"""insert into `tabInn Room Booking`
			   (name, creation, modified, modified_by, owner, docstatus, idx,
			    start, end, room_id, room_availability, reference_type,
			    reference_name, status)
			   values (%s, %s, now(), 'Administrator', 'Administrator', 0, 0,
			           %s, %s, %s, 'Room Sold', 'Inn Reservation', %s, %s)""",
			(name, creation or now(), start or self.start, end or self.end,
			 room, self.RSV, status),
		)
		frappe.db.commit()

	# ------------------------------------------------------------- assertion
	def _move(self):
		return create_move_room_by_reservation(
			reservation_id=self.RSV,
			mv_room_type=self.ROOM_TYPE,
			mv_bed_type=self.BED_TYPE,
			mv_room_id=self.NEW_ROOM,
			mv_reason="T5 fixture",
			mv_change_rate=0,
			mv_room_rate=None,
			mv_actual_room_rate=None,
		)

	def _segment(self, name):
		return frappe.db.get_value(
			"Inn Room Booking", name, ["room_id", "start", "end", "status"], as_dict=True)

	def _new_room_segments(self):
		return frappe.db.sql(
			"""select name, start, end, status from `tabInn Room Booking`
			   where reference_name = %s and room_id = %s""",
			(self.RSV, self.NEW_ROOM), as_dict=True)

	def _rejected_move(self):
		"""Run a move that must be refused, then roll back the way frappe does.

		frappe/app.py:140-192 keeps `rollback = True` when a handler raises and
		calls frappe.db.rollback() in its `finally` for unsafe methods, which is
		what frappe.call posts. There is no request boundary inside a test, so
		the rollback is done here -- the fixture was committed in setUp, so this
		discards only what the move wrote.
		"""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._move()
		error = ctx.exception
		frappe.db.rollback()
		return error

	# ----------------------------------------------------------------- tests
	def test_move_succeeds_when_old_segment_is_booked(self):
		"""The reported failure: a guest whose sync never ran, so 'Booked'.

		RED before the fix: the lookup demanded 'Stayed' and raised
		DoesNotExistError, so the Desk could not move the room at all.
		"""
		self._add_segment("_T5-BK-BOOKED", self.OLD_ROOM, "Booked")

		self.assertEqual(self._move(), 1)

		old = self._segment("_T5-BK-BOOKED")
		self.assertEqual(old.status, "Finished")
		self.assertEqual(getdate(old.end), getdate(self.move_date))

		new = self._new_room_segments()
		self.assertEqual(len(new), 1)
		self.assertEqual(new[0].status, "Stayed")
		self.assertEqual(getdate(new[0].start), getdate(self.move_date))

	def test_move_succeeds_when_old_segment_is_stayed(self):
		"""The normal path must keep working: a guest whose sync did run."""
		self._add_segment("_T5-BK-STAYED", self.OLD_ROOM, "Stayed")

		self.assertEqual(self._move(), 1)

		old = self._segment("_T5-BK-STAYED")
		self.assertEqual(old.status, "Finished")
		self.assertEqual(getdate(old.end), getdate(self.move_date))
		self.assertEqual(len(self._new_room_segments()), 1)

	def test_move_prefers_the_stayed_segment_over_a_booked_duplicate(self):
		"""Widening the lookup must not make it pick a stale 'Booked' duplicate.

		Both segments are in the old room and both span the move date, which is
		what the old room looks like after a move leaves its un-synced segment
		behind. The 'Stayed' one is the guest's live segment.
		"""
		self._add_segment("_T5-BK-BOOKED", self.OLD_ROOM, "Booked",
		                  creation="2026-01-01 09:00:00")
		self._add_segment("_T5-BK-STAYED", self.OLD_ROOM, "Stayed")

		self.assertEqual(self._move(), 1)

		self.assertEqual(self._segment("_T5-BK-STAYED").status, "Finished")
		self.assertEqual(self._segment("_T5-BK-BOOKED").status, "Booked")
		self.assertEqual(len(self._new_room_segments()), 1)

	def test_move_ignores_canceled_and_finished_segments(self):
		"""Past segments are not the guest's live segment."""
		self._add_segment("_T5-BK-CANCELED", self.OLD_ROOM, "Canceled")
		self._add_segment("_T5-BK-FINISHED", self.OLD_ROOM, "Finished")

		error = self._rejected_move()

		# DoesNotExistError subclasses ValidationError, so name the type.
		self.assertNotIsInstance(
			error, frappe.DoesNotExistError,
			"should explain what is missing, not be a bare not-found error")
		message = str(error)
		self.assertIn(self.RSV, message)
		self.assertIn(self.OLD_ROOM, message)
		self.assertNotIn("reference_name", message, "should not leak a filter dict")
		self.assertEqual(frappe.db.count("Inn Move Room", {"reservation_id": self.RSV}), 0)
		self.assertEqual(len(self._new_room_segments()), 0)
		self.assertEqual(
			frappe.db.get_value("Inn Reservation", self.RSV, "actual_room_id"), self.OLD_ROOM)

	def test_move_without_any_segment_reports_a_named_message(self):
		"""No candidate at all is reported in words, and writes nothing.

		RED before the fix: a bare DoesNotExistError naming a filter dict.
		"""
		error = self._rejected_move()

		self.assertNotIsInstance(
			error, frappe.DoesNotExistError,
			"should explain what is missing, not be a bare not-found error")
		message = str(error)
		self.assertIn(self.RSV, message)
		self.assertIn(self.OLD_ROOM, message)
		self.assertNotIn("reference_name", message, "should not leak a filter dict")
		self.assertEqual(frappe.db.count("Inn Move Room", {"reservation_id": self.RSV}), 0)
		self.assertEqual(
			frappe.db.get_value("Inn Reservation", self.RSV, "actual_room_id"), self.OLD_ROOM)

	def test_same_day_move_cancels_the_old_segment(self):
		"""A move on the arrival date allocates no nights to the old room."""
		self._add_segment("_T5-BK-SAMEDAY", self.OLD_ROOM, "Stayed",
		                  start=self.move_date, end=self.end)

		self.assertEqual(self._move(), 1)

		old = self._segment("_T5-BK-SAMEDAY")
		self.assertEqual(old.status, "Canceled")
		self.assertEqual(getdate(old.end), getdate(self.move_date))
		self.assertEqual(len(self._new_room_segments()), 1)

