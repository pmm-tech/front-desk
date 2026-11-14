# -*- coding: utf-8 -*-
# Copyright (c) 2025, PMM and Contributors
# See license.txt
"""DB-backed tests for the Audit Report's row selection.

The predicate is SQL, so a mocked test can only assert the *shape* of the query
-- not which folios it returns. These tests write real ``Inn Folio``,
``Inn Folio Transaction`` and ``Inn Reservation`` records on the test site and
assert the rows the report returns for them.

``test_audit_report.py`` stays DB-free for the classification and row-shaping
logic. This module exists for the predicate alone, and it is the only place the
selection rule is actually executed.

Regression under test: on Jakarta, RSV-26612 (actual arrival 2026-11-07, actual
departure 2026-11-08, one night) appeared in the report for **both** dates,
with a room rate of 0 on the departure date. The departure-date row existed
only because a ``Late Checkout`` debit and its settling payment were audited
that day. One night must produce one row.
"""

from __future__ import unicode_literals

import frappe
from frappe.tests.utils import FrappeTestCase

from inn.helper.revenue_lines import LINE_BREAKFAST, LINE_OTHER, LINE_ROOM
from inn.inn_hotels.report.audit_report import audit_report

#: A date pair the dev bench has no other folio activity on, so seeded data can
#: never make these tests pass or fail for unrelated reasons.
NIGHT = "2030-01-15"
DEPARTURE = "2030-01-16"

#: Passed in rather than read from Inn Hotels Setting, matching the DB-free
#: module's convention. The names are the shipped transaction types.
LINE_MAP = {
    "Room Charge": LINE_ROOM,
    "Room Charge Tax/Service": LINE_ROOM,
    "Breakfast Charge": LINE_BREAKFAST,
    "Late Checkout": LINE_OTHER,
}

_COLUMN_INDEX = {column["fieldname"]: index
                 for index, column in enumerate(audit_report.get_columns())}


def rsv(row):
    """The RSV column of a report row, resolved by name.

    The column order is an interface, so a test that pinned slot numbers would
    have to be rewritten every time a column moved -- which is how a real
    misalignment slips through.
    """
    return row[_COLUMN_INDEX["rsv"]]


def make_folio(folio_type="Guest", reservation=None, customer="TEST-CUST",
               bill_instructions=None):
    doc = frappe.new_doc("Inn Folio")
    # The fixture names links (customer, channel, room type) that do not exist
    # on the test site. insert() takes ignore_links, but save() does not -- it
    # reads the flag off the document -- so set it here for both calls.
    doc.flags.ignore_links = True
    doc.type = folio_type
    doc.customer_id = customer
    doc.reservation_id = reservation
    doc.bill_instructions = bill_instructions
    doc.insert(ignore_permissions=True)
    return doc


def add_transaction(folio, transaction_type, flag, amount, audit_date,
                    mode_of_payment=None, is_void=0, actual_room_rate=None):
    folio.flags.ignore_links = True
    folio.append("folio_transaction", {
        "transaction_type": transaction_type,
        "flag": flag,
        "amount": amount,
        "audit_date": audit_date,
        "mode_of_payment": mode_of_payment,
        "is_void": is_void,
        "actual_room_rate": actual_room_rate,
    })
    folio.save(ignore_permissions=True)
    return folio


def make_reservation(status="Finish", arrival=NIGHT, departure=DEPARTURE):
    doc = frappe.new_doc("Inn Reservation")
    doc.flags.ignore_links = True
    doc.naming_series = "RSV-"
    doc.status = status
    doc.type = "INDIVIDUAL"
    doc.customer_id = "TEST-CUST"
    doc.channel = "Walk In"
    doc.room_type = "Superior"
    doc.bed_type = "Double"
    doc.room_id = "R-1"
    doc.room_rate = "RATE-1"
    doc.init_actual_room_rate = 100
    doc.expected_arrival = arrival
    doc.expected_departure = departure
    doc.insert(ignore_permissions=True)
    return doc


class TestAuditReportSelection(FrappeTestCase):
    """Which folios produce a row, and which do not."""

    def selected(self, on_date=NIGHT):
        return {row.folio for row in audit_report.get_folio_rows(on_date, LINE_MAP)}

    def test_a_room_charge_on_the_date_selects_the_folio(self):
        folio = make_folio()
        add_transaction(folio, "Room Charge", "Debit", 260763, NIGHT)

        self.assertIn(folio.name, self.selected())

    def test_tax_service_alone_on_the_date_selects_the_folio(self):
        folio = make_folio()
        add_transaction(folio, "Room Charge Tax/Service", "Debit", 26076, NIGHT)

        self.assertIn(folio.name, self.selected())

    def test_a_payment_alone_does_not_select_the_folio(self):
        folio = make_folio()
        add_transaction(folio, "Payment", "Credit", 135000, NIGHT,
                        mode_of_payment="BCA QRIS")

        self.assertNotIn(folio.name, self.selected())

    def test_a_late_checkout_alone_does_not_select_the_folio(self):
        folio = make_folio()
        add_transaction(folio, "Late Checkout", "Debit", 135000, NIGHT)

        self.assertNotIn(folio.name, self.selected())

    def test_breakfast_alone_does_not_select_the_folio(self):
        folio = make_folio()
        add_transaction(folio, "Breakfast Charge", "Debit", 72727, NIGHT)

        self.assertNotIn(folio.name, self.selected())

    def test_a_room_charge_on_another_date_does_not_select_today(self):
        folio = make_folio()
        add_transaction(folio, "Room Charge", "Debit", 260763, DEPARTURE)

        self.assertNotIn(folio.name, self.selected(NIGHT))
        self.assertIn(folio.name, self.selected(DEPARTURE))

    def test_a_voided_room_charge_does_not_select_the_folio(self):
        folio = make_folio()
        add_transaction(folio, "Room Charge", "Debit", 260763, NIGHT, is_void=1)

        self.assertNotIn(folio.name, self.selected())

    def test_a_folio_without_a_reservation_names_itself(self):
        folio = make_folio(folio_type="Master")
        add_transaction(folio, "Room Charge", "Debit", 550000, NIGHT)

        rows = {row.folio: row for row in audit_report.get_folio_rows(NIGHT, LINE_MAP)}
        row = rows[folio.name]

        self.assertEqual(row.name, folio.name)
        self.assertEqual(row.room_type, "")
        self.assertEqual(row.bed_type, "")
        self.assertEqual(row.actual_room_id, "")
        self.assertEqual(row.bill_instructions, "Master folio {0}".format(folio.name))

    def test_a_folio_with_a_reservation_renders_the_reservation(self):
        reservation = make_reservation()
        folio = make_folio(reservation=reservation.name)
        add_transaction(folio, "Room Charge", "Debit", 260763, NIGHT)

        rows = {row.folio: row for row in audit_report.get_folio_rows(NIGHT, LINE_MAP)}
        row = rows[folio.name]

        self.assertEqual(row.name, reservation.name)
        self.assertEqual(row.customer_id, reservation.customer_id)
        self.assertEqual(row.room_type, reservation.room_type)
        self.assertEqual(row.bed_type, reservation.bed_type)

    def test_two_folios_for_one_reservation_produce_two_rows(self):
        reservation = make_reservation()
        first = make_folio(reservation=reservation.name)
        second = make_folio(reservation=reservation.name)
        add_transaction(first, "Room Charge", "Debit", 260763, NIGHT)
        add_transaction(second, "Room Charge", "Debit", 50000, NIGHT)

        folios = {row.folio for row in audit_report.get_folio_rows(NIGHT, LINE_MAP)}

        self.assertIn(first.name, folios)
        self.assertIn(second.name, folios)


class TestOneNightOneRow(FrappeTestCase):
    """The RSV-26612 shape, end to end through ``get_data_detail``."""

    def rows_for(self, reservation_name, on_date):
        return [row for row in audit_report.get_data_detail(on_date, 1)
                if rsv(row) == reservation_name]

    def test_a_one_night_stay_appears_on_one_date_only(self):
        reservation = make_reservation()
        folio = make_folio(reservation=reservation.name)

        # Night of 2030-01-15, posted during the 01-16 night audit.
        add_transaction(folio, "Room Charge", "Debit", 260763, NIGHT,
                        actual_room_rate=315524)
        add_transaction(folio, "Room Charge Tax/Service", "Debit", 26076, NIGHT)
        add_transaction(folio, "Room Charge Tax/Service", "Debit", 28685, NIGHT)
        add_transaction(folio, "Room Payment", "Credit", 315524, NIGHT,
                        mode_of_payment="City Ledger")

        # Checkout on 2030-01-16: a late-checkout fee and its settling payment.
        add_transaction(folio, "Late Checkout", "Debit", 135000, DEPARTURE)
        add_transaction(folio, "Payment", "Credit", 135000, DEPARTURE,
                        mode_of_payment="BCA QRIS")

        self.assertEqual(len(self.rows_for(reservation.name, NIGHT)), 1,
                         "the night of the stay must produce exactly one row")
        self.assertEqual(self.rows_for(reservation.name, DEPARTURE), [],
                         "a checkout-day charge must not re-add the stay as a row")
