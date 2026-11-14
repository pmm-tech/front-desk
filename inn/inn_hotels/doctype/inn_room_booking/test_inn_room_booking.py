# -*- coding: utf-8 -*-
# Copyright (c) 2020, Core Initiative and Contributors
# See license.txt
from __future__ import unicode_literals

import unittest

import frappe
from frappe.utils import getdate

from inn.inn_hotels.doctype.inn_room_booking.inn_room_booking import (
	sync_booking_segments_with_reservation,
	update_by_reservation,
)


class MockBooking(object):
	def __init__(self, name, room_id, start, end, status):
		self.name = name
		self.room_id = room_id
		self.start = getdate(start)
		self.end = getdate(end)
		self.status = status
		self.saved = False

	def save(self):
		self.saved = True


class TestInnRoomBooking(unittest.TestCase):
	def test_single_segment_sync_in_house(self):
		"""Single booking segment gets full sync from reservation."""
		booking = MockBooking('BK-1', '101', '2026-10-28', '2026-11-02', 'Booked')
		bookings = [booking]

		sync_booking_segments_with_reservation(
			bookings=bookings,
			reservation_status='In House',
			arrival=getdate('2026-10-28'),
			departure=getdate('2026-11-03'),
			actual_room_id='101',
		)

		self.assertEqual(booking.start, getdate('2026-10-28'))
		self.assertEqual(booking.end, getdate('2026-11-03'))
		self.assertEqual(booking.room_id, '101')
		self.assertEqual(booking.status, 'Stayed')
		self.assertTrue(booking.saved)

	def test_multi_segment_preserves_internal_move_dates(self):
		"""In-house reservation save must not overwrite segment start dates to arrival.

		Old segment (Finished) retains its move-date end boundary.
		New segment (Stayed) retains its move-date start boundary and receives departure update.
		"""
		seg0 = MockBooking('BK-1', '101', '2026-10-28', '2026-10-31', 'Finished')
		seg1 = MockBooking('BK-2', '102', '2026-10-31', '2026-11-02', 'Stayed')
		bookings = [seg0, seg1]

		sync_booking_segments_with_reservation(
			bookings=bookings,
			reservation_status='In House',
			arrival=getdate('2026-10-28'),
			departure=getdate('2026-11-04'),
			actual_room_id='102',
		)

		# Segment 0: room 101, dates 10-28 to 10-31, Finished (unchanged)
		self.assertEqual(seg0.start, getdate('2026-10-28'))
		self.assertEqual(seg0.end, getdate('2026-10-31'))
		self.assertEqual(seg0.room_id, '101')
		self.assertEqual(seg0.status, 'Finished')

		# Segment 1: room 102, start MUST remain 10-31 (NOT overwritten to arrival 10-28!), end extended to 11-04
		self.assertEqual(seg1.start, getdate('2026-10-31'))
		self.assertEqual(seg1.end, getdate('2026-11-04'))
		self.assertEqual(seg1.room_id, '102')
		self.assertEqual(seg1.status, 'Stayed')

	def test_multi_segment_arrival_change_updates_only_first_segment(self):
		"""Changing arrival date affects only segment 0, never intermediate or active segments."""
		seg0 = MockBooking('BK-1', '101', '2026-10-28', '2026-10-31', 'Finished')
		seg1 = MockBooking('BK-2', '102', '2026-10-31', '2026-11-02', 'Stayed')
		bookings = [seg0, seg1]

		sync_booking_segments_with_reservation(
			bookings=bookings,
			reservation_status='In House',
			arrival=getdate('2026-10-27'),
			departure=getdate('2026-11-02'),
			actual_room_id='102',
		)

		self.assertEqual(seg0.start, getdate('2026-10-27'))
		self.assertEqual(seg0.end, getdate('2026-10-31'))
		self.assertEqual(seg1.start, getdate('2026-10-31'))
		self.assertEqual(seg1.end, getdate('2026-11-02'))

	def test_multi_segment_finish_completes_active_segment_only(self):
		"""Checking out (Finish) updates only the active segment to Finished."""
		seg0 = MockBooking('BK-1', '101', '2026-10-28', '2026-10-31', 'Finished')
		seg1 = MockBooking('BK-2', '102', '2026-10-31', '2026-11-02', 'Stayed')
		bookings = [seg0, seg1]

		sync_booking_segments_with_reservation(
			bookings=bookings,
			reservation_status='Finish',
			arrival=getdate('2026-10-28'),
			departure=getdate('2026-11-02'),
			actual_room_id='102',
		)

		self.assertEqual(seg0.status, 'Finished')
		self.assertEqual(seg0.end, getdate('2026-10-31'))
		self.assertEqual(seg1.status, 'Finished')
		self.assertEqual(seg1.end, getdate('2026-11-02'))

	def test_multi_segment_cancel_cancels_all_segments(self):
		"""Canceling reservation cancels all associated booking segments."""
		seg0 = MockBooking('BK-1', '101', '2026-10-28', '2026-10-31', 'Finished')
		seg1 = MockBooking('BK-2', '102', '2026-10-31', '2026-11-02', 'Stayed')
		bookings = [seg0, seg1]

		sync_booking_segments_with_reservation(
			bookings=bookings,
			reservation_status='Cancel',
			arrival=getdate('2026-10-28'),
			departure=getdate('2026-11-02'),
			actual_room_id='102',
		)

		self.assertEqual(seg0.status, 'Canceled')
		self.assertEqual(seg1.status, 'Canceled')


class TestUpdateByReservation(unittest.TestCase):
	"""update_by_reservation is the check-in, check-out and save sync entry point.

	These tests use real DB rows: the defect being pinned lives in the list query
	the function issues, not in the segmentation logic above, so a mock cannot
	reach it.

	Fixture is written with raw SQL and removed in tearDown. It is deliberately
	minimal: no Inn Folio rows, so update_close_by_reservation has nothing to
	close. The Inn Room row exists only because room_id is a Link field and the
	sync saves the booking.
	"""

	RESERVATION = "_T1-RSV-001"
	BOOKING = "_T1-BK-001"
	BOOKING_2 = "_T1-BK-002"
	ROOM = "_T1-R101"
	ROOM_2 = "_T1-R102"

	def setUp(self):
		self._clean()
		for room in (self.ROOM, self.ROOM_2):
			self._add_room(room)
		frappe.db.sql(
			"""insert into `tabInn Reservation`
			   (name, creation, modified, modified_by, owner, docstatus, idx,
			    status, arrival, departure, actual_room_id)
			   values (%s, now(), now(), 'Administrator', 'Administrator', 0, 0,
			           'In House', '2026-11-10', '2026-11-13', %s)""",
			(self.RESERVATION, self.ROOM),
		)
		self._add_segment(self.BOOKING, self.ROOM, '2026-11-10', '2026-11-13',
		                  'Booked', '2026-11-01 10:00:00')
		frappe.db.commit()

	def tearDown(self):
		self._clean()

	def _clean(self):
		frappe.db.sql("delete from `tabInn Room Booking` where reference_name = %s",
		              self.RESERVATION)
		frappe.db.sql("delete from `tabInn Reservation` where name = %s", self.RESERVATION)
		frappe.db.sql("delete from `tabInn Room` where name in (%s, %s)",
		              (self.ROOM, self.ROOM_2))
		frappe.db.commit()

	def _add_room(self, name):
		frappe.db.sql(
			"""insert into `tabInn Room`
			   (name, creation, modified, modified_by, owner, docstatus, idx)
			   values (%s, now(), now(), 'Administrator', 'Administrator', 0, 0)""",
			name,
		)

	def _add_segment(self, name, room, start, end, status, creation):
		"""Insert a segment with a chosen `creation`, so order tests can make the
		start order and the creation order disagree."""
		frappe.db.sql(
			"""insert into `tabInn Room Booking`
			   (name, creation, modified, modified_by, owner, docstatus, idx,
			    start, end, room_id, room_availability, reference_type,
			    reference_name, status)
			   values (%s, %s, now(), 'Administrator', 'Administrator', 0, 0,
			           %s, %s, %s, 'Room Sold', 'Inn Reservation', %s, %s)""",
			(name, creation, start, end, room, self.RESERVATION, status),
		)

	def _booking(self, name=None):
		return frappe.db.get_value(
			'Inn Room Booking', name or self.BOOKING,
			['start', 'end', 'room_id', 'status', 'creation'], as_dict=True)

	def test_in_house_reservation_syncs_segment_without_error(self):
		"""A checked-in reservation must sync its segment, not raise.

		RED before the fix: the order_by in update_by_reservation named the
		column `start`, which sqlparse types as Keyword.DML, so frappe's list
		query guard rejected the query and the Desk showed "Illegal SQL query"
		after an otherwise successful check-in.
		"""
		update_by_reservation(self.RESERVATION)

		booking = self._booking()
		self.assertEqual(booking.status, 'Stayed')
		self.assertEqual(getdate(booking.start), getdate('2026-11-10'))
		self.assertEqual(getdate(booking.end), getdate('2026-11-13'))
		self.assertEqual(booking.room_id, self.ROOM)

	def test_finish_reservation_syncs_segment_without_error(self):
		"""Checking out must finish the segment, not raise."""
		frappe.db.set_value('Inn Reservation', self.RESERVATION, 'status', 'Finish')

		update_by_reservation(self.RESERVATION)

		self.assertEqual(self._booking().status, 'Finished')

	def test_cancel_reservation_syncs_segment_without_error(self):
		"""Cancelling a checked-in reservation must cancel the segment, not raise."""
		frappe.db.set_value('Inn Reservation', self.RESERVATION, 'status', 'Cancel')

		update_by_reservation(self.RESERVATION)

		self.assertEqual(self._booking().status, 'Canceled')

	def test_segments_are_ordered_by_start_not_by_creation(self):
		"""The earliest-start segment is the first, even when created last.

		The two orderings are made to disagree: the fixture segment starts
		2026-11-10 but was created 2026-11-05, while the added one starts
		2026-11-11 and was created 2026-11-01. Ordering by `creation` would sync
		arrival to the wrong segment and departure to the wrong one, so these
		assertions discriminate between the two.
		"""
		self._add_segment(self.BOOKING_2, self.ROOM_2, '2026-11-11', '2026-11-12',
		                  'Stayed', '2026-11-01 09:00:00')
		frappe.db.set_value('Inn Room Booking', self.BOOKING, 'creation', '2026-11-05 09:00:00')
		frappe.db.set_value('Inn Reservation', self.RESERVATION, 'arrival', '2026-11-09')
		frappe.db.set_value('Inn Reservation', self.RESERVATION, 'departure', '2026-11-14')
		frappe.db.set_value('Inn Reservation', self.RESERVATION, 'actual_room_id', self.ROOM_2)
		frappe.db.commit()

		update_by_reservation(self.RESERVATION)

		first = self._booking(self.BOOKING)
		last = self._booking(self.BOOKING_2)
		# arrival goes to the earliest start ...
		self.assertEqual(getdate(first.start), getdate('2026-11-09'))
		# ... and departure to the latest start ...
		self.assertEqual(getdate(last.end), getdate('2026-11-14'))
		# ... leaving the other end of each segment alone.
		self.assertEqual(getdate(first.end), getdate('2026-11-13'))
		self.assertEqual(getdate(last.start), getdate('2026-11-11'))

	def test_reservation_without_bookings_returns_empty_message(self):
		"""No segments is not an error: the function reports nothing to sync."""
		frappe.db.sql("delete from `tabInn Room Booking` where reference_name = %s",
		              self.RESERVATION)
		frappe.db.commit()

		self.assertEqual(update_by_reservation(self.RESERVATION), '')
