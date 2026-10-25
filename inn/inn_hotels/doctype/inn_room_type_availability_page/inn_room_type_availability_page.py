# -*- coding: utf-8 -*-
# Copyright (c) 2021, Core Initiative and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

from datetime import date, datetime, timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from inn.helper.daterange import daterange

# Rooms without a Bed Type are reported in one bucket. '' rather than a label
# keeps the API language-neutral; the desk script renders the dash.
UNSPECIFIED = ""

# The page renders a column per day, so an unbounded range would both widen the
# table past readability and scan the whole booking history.
MAX_RANGE_DAYS = 92

ROOM_TYPE_TOTALS_SQL = """
	SELECT
		room_type,
		IFNULL(bed_type, '') AS bed_type,
		COUNT(*) AS total
	FROM `tabInn Room`
	WHERE IFNULL(room_type, '') != ''
	GROUP BY room_type, IFNULL(bed_type, '')
"""

ROOM_TYPE_BOOKINGS_SQL = """
	SELECT
		room.room_type AS room_type,
		IFNULL(room.bed_type, '') AS bed_type,
		booking.start AS start,
		booking.end AS end
	FROM `tabInn Room Booking` booking
	INNER JOIN `tabInn Room` room ON room.name = booking.room_id
	WHERE booking.status IN ('Booked', 'Stayed')
		AND booking.start IS NOT NULL
		AND booking.end IS NOT NULL
		AND booking.start <= %(end)s
		AND booking.end > %(start)s
		AND IFNULL(room.room_type, '') != ''
"""


class InnRoomTypeAvailabilityPage(Document):
	pass


@frappe.whitelist()
def get_room_type_bed_type_availability(start, end):
	"""Availability per room type and bed type, for every day in the range.

	The range is inclusive on both ends and capped at MAX_RANGE_DAYS.
	"""
	start_date = _require_date(start, _("Start Date"))
	end_date = _require_date(end, _("End Date"))

	if end_date < start_date:
		frappe.throw(_("End Date cannot be before Start Date"))

	if (end_date - start_date).days + 1 > MAX_RANGE_DAYS:
		frappe.throw(_("Please select a range of {0} days or fewer").format(MAX_RANGE_DAYS))

	dates = build_window_dates(start_date, end_date)
	room_types = [row.name for row in frappe.get_all("Inn Room Type", fields=["name"], order_by="name asc")]
	room_totals = frappe.db.sql(ROOM_TYPE_TOTALS_SQL, as_dict=True)
	bookings = frappe.db.sql(ROOM_TYPE_BOOKINGS_SQL, {"start": start_date, "end": end_date}, as_dict=True)

	return {
		"dates": [day.isoformat() for day in dates],
		"rows": build_availability_rows(room_types, room_totals, bookings, dates),
	}


def build_window_dates(start_date, end_date):
	"""Every day from ``start_date`` to ``end_date``, both included."""
	return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def build_availability_rows(room_types, room_totals, bookings, dates):
	"""Shape the availability matrix the page renders.

	One row per (room type, bed type). ``available`` always carries a key for
	every requested date, so the page can render a cell per column without
	guessing. A booking's ``end`` is the checkout day and stays sellable; a
	bucket can go negative when a room type is overbooked and that is reported
	as-is rather than floored at zero.
	"""
	date_keys = [day.isoformat() for day in dates]
	requested = set(date_keys)

	totals = {}
	for row in room_totals:
		room_type = row.get("room_type")
		if room_type is None:
			continue
		key = (room_type, _bed_type(row.get("bed_type")))
		totals[key] = totals.get(key, 0) + int(row.get("total") or 0)

	booked = {}
	for row in bookings:
		room_type = row.get("room_type")
		if room_type is None:
			continue
		start_date = _to_date(row.get("start"))
		end_date = _to_date(row.get("end"))
		if start_date is None or end_date is None:
			continue
		counts = booked.setdefault((room_type, _bed_type(row.get("bed_type"))), {})
		for day in daterange(start_date, end_date):
			day_key = day.isoformat()
			if day_key in requested:
				counts[day_key] = counts.get(day_key, 0) + 1

	# Room types come from the configured list, but a booking or a room pointing
	# at an unlisted type still gets a row instead of being dropped.
	room_type_names = {name for name in room_types if name}
	room_type_names.update(room_type for room_type, _bed in totals)
	room_type_names.update(room_type for room_type, _bed in booked)

	rows = []
	for room_type in sorted(room_type_names):
		bed_types = {bed for (room, bed) in totals if room == room_type}
		bed_types.update(bed for (room, bed) in booked if room == room_type)
		if not bed_types:
			bed_types = {UNSPECIFIED}

		for bed_type in sorted(bed_types, key=lambda bed: (bed == UNSPECIFIED, bed)):
			total = totals.get((room_type, bed_type), 0)
			counts = booked.get((room_type, bed_type), {})
			rows.append(
				{
					"room_type": room_type,
					"bed_type": bed_type,
					"total": total,
					"available": {day_key: total - counts.get(day_key, 0) for day_key in date_keys},
				}
			)

	return rows


def _bed_type(value):
	return (value or "").strip()


def _to_date(value):
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	if isinstance(value, str) and value.strip():
		try:
			return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
		except ValueError:
			return None
	return None


def _require_date(value, label):
	try:
		parsed = getdate(value)
	except Exception:
		parsed = None

	if not parsed:
		frappe.throw(_("{0} is required").format(label))

	return parsed
