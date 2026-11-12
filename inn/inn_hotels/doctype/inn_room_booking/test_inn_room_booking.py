# -*- coding: utf-8 -*-
# Copyright (c) 2020, Core Initiative and Contributors
# See license.txt
from __future__ import unicode_literals

import unittest
from frappe.utils import getdate

from inn.inn_hotels.doctype.inn_room_booking.inn_room_booking import (
	sync_booking_segments_with_reservation,
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
