# -*- coding: utf-8 -*-
# Copyright (c) 2020, Core Initiative and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.document import Document
from frappe.utils import getdate
from inn.inn_hotels.doctype.inn_folio.inn_folio import update_close_by_reservation
from datetime import date
from dateutil.parser import parse


class InnRoomBooking(Document):

    def on_update(self):
        if frappe.db.exists("Inn Guest Booking Room", {"inn_room_booking": self.name}, cache=True):
            gbr = frappe.get_doc("Inn Guest Booking Room", {
                                 "inn_room_booking": self.name}, cache=True)
            # resave to fetch value from inn room booking to inn guest booking room
            gbr.save()

        if self.room_availability in ["Out of Order", "Under Construction"]:
            if not (self.start <= date.today() < self.end):
                return

            if self.status == "Booked":
                # set inn room status as out of order
                # because from the flow, any room can be changed to "Out of Order" without any validation
                # this changes wont check any pre-existing condition
                room_status = "Out of Order"
            elif self.status in ["Finished", "Canceled"]:
                # set inn room status as vacant dirty
                # what if there are multiple OOO & UC in one room in sequential? (different object)?
                # this time, don't care it will still resolve room as Vacant Dirty
                room_status = "Vacant Dirty"

            frappe.db.set_value('Inn Room', self.room_id,
                                'room_status', room_status)


def sync_booking_segments_with_reservation(bookings, reservation_status, arrival, departure, actual_room_id=None):
    """Synchronize room booking segments with reservation status and dates.

    Protects multi-segment reservations (created by room moves) from having
    internal segment boundaries overwritten.
    - Status 'Cancel' / 'No Show': Cancels all segments.
    - Status 'Finish': Marks only the last non-canceled segment as Finished and sets its end to departure.
    - Status 'In House':
      * Single segment: syncs start=arrival, end=departure, room_id=actual_room_id, status='Stayed'.
      * Multi-segment: syncs arrival ONLY to the first non-canceled segment.
        Syncs departure, actual_room_id, status='Stayed' ONLY to the last non-canceled segment.
        Intermediate move dates and segment rooms are strictly preserved.
    """
    if not bookings:
        return

    if arrival:
        arrival = getdate(arrival)
    if departure:
        departure = getdate(departure)

    if reservation_status in ['Cancel', 'No Show']:
        for booking in bookings:
            if booking.status != 'Canceled':
                booking.status = 'Canceled'
                booking.save()
        return

    non_canceled = [b for b in bookings if b.status != 'Canceled']
    if not non_canceled:
        non_canceled = bookings

    if reservation_status == 'Finish':
        last_booking = non_canceled[-1]
        changed = False
        if last_booking.status != 'Finished':
            last_booking.status = 'Finished'
            changed = True
        if departure and getdate(last_booking.end) != departure:
            last_booking.end = departure
            changed = True
        if changed:
            last_booking.save()
        return

    if reservation_status == 'In House':
        if len(non_canceled) == 1:
            booking = non_canceled[0]
            changed = False
            if arrival and getdate(booking.start) != arrival:
                booking.start = arrival
                changed = True
            if departure and getdate(booking.end) != departure:
                booking.end = departure
                changed = True
            if actual_room_id and booking.room_id != actual_room_id:
                booking.room_id = actual_room_id
                changed = True
            if booking.status != 'Stayed':
                booking.status = 'Stayed'
                changed = True
            if changed:
                booking.save()
        else:
            first_booking = non_canceled[0]
            last_booking = non_canceled[-1]

            if arrival and getdate(first_booking.start) != arrival:
                first_booking.start = arrival
                first_booking.save()

            last_changed = False
            if departure and getdate(last_booking.end) != departure:
                last_booking.end = departure
                last_changed = True
            if actual_room_id and last_booking.room_id != actual_room_id:
                last_booking.room_id = actual_room_id
                last_changed = True
            if last_booking.status != 'Stayed':
                last_booking.status = 'Stayed'
                last_changed = True
            if last_changed:
                last_booking.save()


@frappe.whitelist()
# Keep the Room Booking valid when Reservation created or updated
def update_by_reservation(reservation_id):
    return_message = ''
    reservation_doc = frappe.get_doc('Inn Reservation', reservation_id)

    # handle error when creating reservation
    if reservation_doc.status != "Reserved":
        # Update Folio Close if Reservation get Updated
        update_close_by_reservation(reservation_id)

    if reservation_doc.status in ['Cancel', 'Finish', 'In House', 'No Show']:
        booking_names = frappe.get_all(
            'Inn Room Booking',
            filters={'reference_type': 'Inn Reservation', 'reference_name': reservation_id},
            order_by='start asc, creation asc',
            pluck='name'
        )
        bookings = [frappe.get_doc('Inn Room Booking', name) for name in booking_names]
        if bookings:
            sync_booking_segments_with_reservation(
                bookings=bookings,
                reservation_status=reservation_doc.status,
                arrival=reservation_doc.arrival,
                departure=reservation_doc.departure,
                actual_room_id=reservation_doc.actual_room_id,
            )
            return_message = 'Synchronized {0} Room Bookings for Reservation: {1}'.format(
                len(bookings), reservation_id
            )
    else:
        # Reservation status is Reserved
        if not frappe.db.exists('Inn Room Booking',
                                {'reference_type': 'Inn Reservation', 'reference_name': reservation_id}):
            # Room Booking is not created yet. New Reservation with status Reserved, create new Room Booking with the info
            # from Reservation details and Status is Booked
            room_booking_doc = frappe.new_doc('Inn Room Booking')
            room_booking_doc.start = reservation_doc.expected_arrival
            room_booking_doc.end = reservation_doc.expected_departure
            room_booking_doc.room_id = reservation_doc.room_id
            room_booking_doc.room_availability = 'Room Sold'
            room_booking_doc.reference_type = 'Inn Reservation'
            room_booking_doc.reference_name = reservation_id
            room_booking_doc.status = 'Booked'
            room_booking_doc.insert()
            return_message = 'Created New Room Booking: ' + room_booking_doc.name
        else:
            # Update room booking if reservation got updated, while guest is still not Checking In
            room_booking_doc = frappe.get_doc('Inn Room Booking',
                                              {'reference_type': 'Inn Reservation', 'reference_name': reservation_id})
            reservation_changed = False
            if reservation_doc.expected_arrival != room_booking_doc.start:
                reservation_changed = True
                room_booking_doc.start = reservation_doc.expected_arrival
            if reservation_doc.expected_departure != room_booking_doc.end:
                reservation_changed = True
                room_booking_doc.end = reservation_doc.expected_departure
            if reservation_doc.room_id != room_booking_doc.room_id:
                reservation_changed = True
                room_booking_doc.room_id = reservation_doc.room_id
            if reservation_doc.actual_room_id != None and reservation_doc.actual_room_id != room_booking_doc.room_id:
                # will use the actual usage of room regards less of reserved room, because actual room takes precedence over a reservation room
                # this flow in handled when check-in but the flow also needed in save as reservation. 'if user can save it,  handle it'
                reservation_changed = True
                room_booking_doc.room_id = reservation_doc.actual_room_id
            if reservation_changed:
                room_booking_doc.save()
                return_message = room_booking_doc.name + \
                    ' Updated by changes in Reservation: ' + reservation_doc.name
    return return_message


@frappe.whitelist()
def get_room_available(doctype, txt, searchfield, start, page_len, filters):
    room_list = []
    if filters.get('phase') == 'Check In':
        if filters.get('start') and filters.get('end'):
            if filters.get('room_type'):
                if filters.get('bed_type'):
                    room_list = list(frappe.db.sql(
                        "select name, room_type, bed_type from `tabInn Room` where room_status = 'Vacant Ready' and room_type = %s and bed_type = %s",
                        (filters.get('room_type'), filters.get('bed_type'))
                    ))
                else:
                    room_list = list(frappe.db.sql(
                        "select name, room_type, bed_type from `tabInn Room` where room_status = 'Vacant Ready' and room_type = %s",
                        (filters.get('room_type'))
                    ))
            else:
                room_list = list(frappe.db.sql(
                    "select name, room_type, bed_type from `tabInn Room` where room_status = 'Vacant Ready' "
                ))
    else:
        if filters.get('start') and filters.get('end'):
            if filters.get('room_type'):
                if filters.get('bed_type'):
                    room_list = list(frappe.db.sql(
                        "select name, room_type, bed_type from `tabInn Room` where room_type = %s and bed_type = %s",
                        (filters.get('room_type'), filters.get('bed_type'))
                    ))
                else:
                    room_list = list(frappe.db.sql(
                        "select name, room_type, bed_type from `tabInn Room` where room_type = %s",
                        (filters.get('room_type'))
                    ))
            else:
                room_list = list(frappe.db.sql(
                    "select name, room_type, bed_type from `tabInn Room`"
                ))

    room_book_list = get_room_book_list(filters)

    for room_book in room_book_list:
        for item in room_list:
            if item[0] == room_book[0]:
                room_list.remove(item)

    return room_list


@frappe.whitelist()
def get_room_type_available(doctype, txt, searchfield, start, page_len, filters):
    room_list = get_room_available(
        doctype, txt, searchfield, start, page_len, filters)

    tmp = []
    room_type_list = []

    for room in room_list:
        tmp.append(room[1])

    tmp = list(set(tmp))

    for t in tmp:
        room_type_list.append([t])

    return room_type_list


@frappe.whitelist()
def get_bed_type_available(doctype, txt, searchfield, start, page_len, filters):
    room_list = get_room_available(
        doctype, txt, searchfield, start, page_len, filters)

    tmp = []
    bed_type_list = []

    for room in room_list:
        if room[1] == filters.get('room_type'):
            tmp.append(room[2])

    tmp = list(set(tmp))

    for t in tmp:
        bed_type_list.append([t])

    return bed_type_list


def get_room_book_list(filters):
    room_book_list = list(frappe.db.sql(
        "select irb.room_id from `tabInn Room Booking` as irb "
        "where irb.reference_name != %s "
        "and irb.status = 'Booked' "
        "and irb.room_availability = 'Room Sold' "
        "and irb.start != irb.end "
        "and ((%s >= irb.start and %s < irb.end) "
        "or (%s > irb.start and %s <= irb.end) "
        "or (%s < irb.start and %s > irb.end))",
        (filters.get('reference_name'),
         filters.get('start'), filters.get('start'),
         filters.get('end'), filters.get('end'),
         filters.get('start'), filters.get('end'))
    ))
    room_book_list.extend(list(frappe.db.sql(
        "select irb.room_id from `tabInn Room Booking` as irb "
        "where irb.status = 'Booked' "
        "and irb.room_availability in ('Under Construction', 'Office Use', 'Out of Order', 'House Use', 'Room Compliment') "
        "and irb.start != irb.end "
        "and ((%s >= irb.start and %s < irb.end) "
        "or (%s > irb.start and %s <= irb.end) "
        "or (%s < irb.start and %s > irb.end))",
        (filters.get('start'), filters.get('start'),
         filters.get('end'), filters.get('end'),
         filters.get('start'), filters.get('end'))
    )))
    return room_book_list


@frappe.whitelist()
def get_all_room_with_room_booking_status():
    return_list = []
    room_list = frappe.get_all('Inn Room', fields=['*'])
    room_booking_list = frappe.get_all('Inn Room Booking', filters={'status': [
                                       'in', ['Booked', 'Stayed']]}, fields=['*'])

    for room in room_list:
        return_item = {'name': room.name, 'status': 'AV'}
        for room_booking in room_booking_list:
            if room_booking.room_id == room.name:
                if room_booking.room_availability == 'Room Sold':
                    return_item['status'] = 'RS'
                elif room_booking.room_availability == 'Under Construction':
                    return_item['status'] = 'UC'
                elif room_booking.room_availability == 'Office Use':
                    return_item['status'] = 'OU'
                elif room_booking.room_availability == 'Out of Order':
                    return_item['status'] = 'OO'
                elif room_booking.room_availability == 'House Use':
                    return_item['status'] = 'HU'
                elif room_booking.room_availability == 'Room Compliment':
                    return_item['status'] = 'RC'
            else:
                pass
        return_list.append(return_item)

    return return_list


@frappe.whitelist()
def is_available(room_id, start, end, name):
    if name:
        result = frappe.db.sql(
            'SELECT room_id FROM `tabInn Room Booking` '
            'WHERE %s != name AND %s = room_id AND status = "Booked" AND (start != end) AND '
            '((%s >= start AND %s < end) OR (%s > start AND %s <= end) OR (%s < start AND %s > end))',
            (name, room_id, start, start, end, end, start, end)
        )
    else:
        result = frappe.db.sql(
            'SELECT room_id FROM `tabInn Room Booking` '
            'WHERE %s = room_id AND status = "Booked" AND (start != end) AND '
            '((%s >= start AND %s < end) OR (%s > start AND %s <= end) OR (%s < start AND %s > end))',
            (room_id, start, start, end, end, start, end)
        )

    if result:
        return False
    else:
        return True


@frappe.whitelist()
def get_name_within_date_range(room_id, start, end):
    result = frappe.db.sql(
        'SELECT name FROM `tabInn Room Booking` '
        'WHERE %s = room_id AND (status = "Booked" OR status = "Stayed") AND (start != end) AND '
        '((%s >= start AND %s < end) OR (%s > start AND %s <= end) OR (%s < start AND %s > end))',
        (room_id, start, start, end, end, start, end)
    )
    return result


@frappe.whitelist()
def get_room_booking_name_by_reservation(reservation_id):
    room_booking = frappe.get_doc('Inn Room Booking',
                                  {'reference_type': 'Inn Reservation', 'reference_name': reservation_id})
    if room_booking:
        return room_booking.name
    else:
        return None


@frappe.whitelist()
def get_room_booking(room_id, date):
    return frappe.db.sql(
        'SELECT name, start, end, room_availability, note FROM `tabInn Room Booking` WHERE status = "Booked" AND room_id = %s AND %s >= start AND %s < end',
        (room_id, date, date))
