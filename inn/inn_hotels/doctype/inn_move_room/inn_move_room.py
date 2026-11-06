# -*- coding: utf-8 -*-
# Copyright (c) 2020, Core Initiative and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import datetime
import frappe
from frappe.model.document import Document
from frappe.utils import getdate
from inn.inn_hotels.doctype.inn_room_rate.inn_room_rate import get_actual_room_rate_breakdown_check_commission


def calculate_room_move_booking_boundaries(old_start, old_end, move_date=None):
	"""Calculate date boundaries and statuses for room move booking segments.

	Ensures that:
	1. Mid-stay move (move_date > old_start): old booking ends at move_date ('Finished'),
	   new booking starts at move_date and ends at original departure ('Stayed').
	2. Same-day move (move_date == old_start): guest spent 0 nights in old room.
	   Old booking is canceled ('Canceled') so it produces 0 room nights in reports.
	   New booking starts at move_date and ends at original departure ('Stayed').
	3. Strict validation: move_date must satisfy old_start <= move_date < old_end.
	"""
	if move_date is None:
		move_date = datetime.date.today()
	else:
		move_date = getdate(move_date)

	old_start = getdate(old_start)
	old_end = getdate(old_end)

	if move_date < old_start:
		frappe.throw(
			frappe._("Move date ({0}) cannot be earlier than booking start date ({1})").format(
				move_date, old_start
			),
			frappe.ValidationError,
		)

	if move_date >= old_end:
		frappe.throw(
			frappe._("Move date ({0}) cannot be on or after booking departure date ({1})").format(
				move_date, old_end
			),
			frappe.ValidationError,
		)

	if move_date == old_start:
		old_status = "Canceled"
	else:
		old_status = "Finished"

	return {
		"old_status": old_status,
		"old_end": move_date,
		"new_start": move_date,
		"new_end": old_end,
		"new_status": "Stayed",
	}


class InnMoveRoom(Document):
	pass


@frappe.whitelist()
def create_move_room_by_reservation(reservation_id, mv_room_type, mv_bed_type, mv_room_id, mv_reason, mv_change_rate,
									mv_room_rate, mv_actual_room_rate):

	reservation_doc = frappe.get_doc('Inn Reservation', reservation_id)
	# Create new Move Room
	move_room_doc = frappe.new_doc('Inn Move Room')
	move_room_doc.reservation_id = reservation_id
	move_room_doc.old_room_id = reservation_doc.actual_room_id
	move_room_doc.arrival = datetime.date.today()
	move_room_doc.departure = reservation_doc.departure
	move_room_doc.room_type = mv_room_type
	move_room_doc.bed_type = mv_bed_type
	move_room_doc.new_room = mv_room_id
	move_room_doc.reason = mv_reason
	move_room_doc.change_rate = mv_change_rate
	move_room_doc.old_room_rate = reservation_doc.room_rate
	move_room_doc.old_actual_rate = float(reservation_doc.actual_room_rate)
	if int(mv_change_rate) == 1:
		move_room_doc.new_room_rate = mv_room_rate
		move_room_doc.new_actual_rate = float(mv_actual_room_rate)

	move_room_doc.insert()

	# Update reservation
	reservation_doc.actual_room_id = mv_room_id
	if int(mv_change_rate) == 1:
		room_rate_tax, room_rate, breakfast_tax, breakfast_rate = get_actual_room_rate_breakdown_check_commission(mv_room_rate,
																								 mv_actual_room_rate, reservation_id)
		reservation_doc.room_rate = mv_room_rate
		reservation_doc.actual_room_rate = float(mv_actual_room_rate)
		reservation_doc.actual_room_rate_tax = room_rate_tax
		reservation_doc.nett_actual_room_rate = float(room_rate)
		reservation_doc.actual_breakfast_rate_tax = breakfast_tax
		reservation_doc.nett_actual_breakfast_rate = float(breakfast_rate)

	# Deactivate all Inn Key Card Issued
	key_list = reservation_doc.issued_card
	for key in key_list:
		if key.is_active == 1:
			key.is_active = 0
			key.save()
	reservation_doc.save()

	# Update room booking
	old_room_booking = frappe.get_doc('Inn Room Booking',
									  {'reference_name': reservation_id, 'room_id': move_room_doc.old_room_id,
									   'status': 'Stayed'})

	if move_room_doc.old_room_id != move_room_doc.new_room:
		boundaries = calculate_room_move_booking_boundaries(
			old_room_booking.start,
			old_room_booking.end,
			datetime.date.today(),
		)
		old_room_booking.status = boundaries['old_status']
		old_room_booking.end = boundaries['old_end']
		old_room_booking.save()

		new_room_booking = frappe.new_doc('Inn Room Booking')
		new_room_booking.start = boundaries['new_start']
		new_room_booking.end = boundaries['new_end']
		new_room_booking.room_id = move_room_doc.new_room
		new_room_booking.room_availability = 'Room Sold'
		new_room_booking.reference_type = 'Inn Reservation'
		new_room_booking.reference_name = reservation_id
		new_room_booking.status = boundaries['new_status']
		new_room_booking.insert()

	# Update Rooms Status
	old_room = frappe.get_doc('Inn Room', move_room_doc.old_room_id)
	new_room = frappe.get_doc('Inn Room', mv_room_id)

	old_room.room_status = 'Vacant Dirty'
	old_room.door_status = 'No Status'
	old_room.save()

	new_room.room_status = 'Occupied Clean'
	new_room.save()

	return 1
