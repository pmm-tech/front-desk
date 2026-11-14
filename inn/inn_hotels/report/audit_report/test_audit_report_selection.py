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

#: Dates the dev bench has no other folio activity on, so seeded data can never
#: make these tests pass or fail for unrelated reasons.
BEFORE = "2030-01-14"
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


def cell(row, fieldname):
    """A report row's value, resolved by column name.

    The column order is an interface, so a test that pinned slot numbers would
    have to be rewritten every time a column moved -- which is how a real
    misalignment slips through.
    """
    return row[_COLUMN_INDEX[fieldname]]


def rsv(row):
    return cell(row, "rsv")


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
                    mode_of_payment=None, is_void=0, actual_room_rate=None,
                    remark=None):
    folio.flags.ignore_links = True
    folio.append("folio_transaction", {
        "transaction_type": transaction_type,
        "flag": flag,
        "amount": amount,
        "audit_date": audit_date,
        "mode_of_payment": mode_of_payment,
        "is_void": is_void,
        "actual_room_rate": actual_room_rate,
        "remark": remark,
    })
    folio.save(ignore_permissions=True)
    return folio


def set_creation(folio, transaction_type, created):
    """Force a transaction's wall-clock ``creation``.

    Frappe stamps ``creation`` with the real clock, so every row a fixture
    inserts in one test shares today's timestamp. Two columns depend on that
    clock -- ``posting_date`` and ``paid_date`` -- so a fixture that cannot
    separate their timestamps cannot test them. Real rows carry a creation on
    their business date, so this makes the fixture more realistic, not less.
    """
    name = frappe.db.get_value(
        "Inn Folio Transaction",
        {"parent": folio.name, "transaction_type": transaction_type}, "name")
    frappe.db.set_value("Inn Folio Transaction", name, "creation", created)
    return created


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


class TestPaymentModeColumn(FrappeTestCase):
    """Mode of Payment describes the guest's whole stay, not one date.

    Before this change the column was built from the transactions audited on
    the report date, so a guest who settled at checkout showed only the
    checkout-day mode -- and on RSV-26612 the night's row showed City Ledger
    while the QRIS payment vanished onto a second, phantom row.
    """

    def mode_cell(self, reservation_name, on_date, show_modes=1):
        rows = [row for row in audit_report.get_data_detail(on_date, show_modes)
                if rsv(row) == reservation_name]
        self.assertEqual(len(rows), 1, "expected exactly one row for the stay")
        return rows[0][_COLUMN_INDEX["mode_of_payment"]]

    def settled_stay(self, mode, amount=135000, on_date=DEPARTURE, is_void=0):
        reservation = make_reservation()
        folio = make_folio(reservation=reservation.name)
        add_transaction(folio, "Room Charge", "Debit", 260763, NIGHT)
        add_transaction(folio, "Payment", "Credit", amount, on_date,
                        mode_of_payment=mode, is_void=is_void)
        return reservation

    def test_a_payment_after_the_report_date_reaches_the_nights_row(self):
        reservation = self.settled_stay("BCA QRIS")

        self.assertEqual(self.mode_cell(reservation.name, NIGHT), "BCA QRIS")

    def test_a_deposit_taken_before_arrival_reaches_the_nights_row(self):
        reservation = self.settled_stay("BCA Online Transfer", on_date=BEFORE)

        self.assertEqual(self.mode_cell(reservation.name, NIGHT),
                         "BCA Online Transfer")

    def test_modes_are_deduplicated_and_ordered_by_first_payment(self):
        reservation = make_reservation()
        folio = make_folio(reservation=reservation.name)
        add_transaction(folio, "Room Charge", "Debit", 260763, NIGHT)
        add_transaction(folio, "Room Payment", "Credit", 100000, NIGHT,
                        mode_of_payment="QRIS")
        add_transaction(folio, "Payment", "Credit", 50000, NIGHT,
                        mode_of_payment="CASH")
        add_transaction(folio, "Payment", "Credit", 50000, DEPARTURE,
                        mode_of_payment="QRIS")

        # QRIS was paid first, so it leads. "CASH, QRIS" would be the
        # alphabet's order, not the guest's.
        self.assertEqual(self.mode_cell(reservation.name, NIGHT), "QRIS, CASH")

    def test_a_voided_credit_contributes_no_mode(self):
        reservation = self.settled_stay("VOIDED MODE", is_void=1)

        self.assertEqual(self.mode_cell(reservation.name, NIGHT), "")

    def test_the_column_is_suppressed_when_the_filter_is_off(self):
        reservation = self.settled_stay("CASH")

        self.assertEqual(self.mode_cell(reservation.name, NIGHT, show_modes=0), "")


class TestMoneyColumns(FrappeTestCase):
    """Total Amount is the gross room charge; the dead columns stay zero.

    The report was a record of the day's postings, so Total Amount held the day's
    collections and Breakfast / Comission held their own revenue. It is now a
    room-charge ledger: Total Amount holds the charge, and non-room money is
    Front Desk Daily Revenue Recap's job.
    """

    def only_row(self, reservation_name, on_date=NIGHT):
        rows = [row for row in audit_report.get_data_detail(on_date, 1)
                if rsv(row) == reservation_name]
        self.assertEqual(len(rows), 1, "expected exactly one row for the stay")
        return rows[0]

    def charge_folio(self, gross_nett=260763, taxes=(26076, 28685), rate=315524):
        reservation = make_reservation()
        folio = make_folio(reservation=reservation.name)
        add_transaction(folio, "Room Charge", "Debit", gross_nett, NIGHT,
                        actual_room_rate=rate)
        for tax in taxes:
            add_transaction(folio, "Room Charge Tax/Service", "Debit", tax, NIGHT)
        return reservation, folio

    def test_total_amount_is_the_gross_room_charge(self):
        reservation, _ = self.charge_folio()

        row = self.only_row(reservation.name)

        self.assertEqual(cell(row, "actual_room_nett"), 260763)
        self.assertEqual(cell(row, "total_amount"), 315524)
        self.assertEqual(cell(row, "actual_room_rate"), 315524)
        self.assertEqual(cell(row, "total_amount"), 260763 + 26076 + 28685)

    def test_a_payment_is_not_in_total_amount(self):
        reservation, folio = self.charge_folio(taxes=())
        add_transaction(folio, "Payment", "Credit", 500000, NIGHT,
                        mode_of_payment="CASH")

        self.assertEqual(cell(self.only_row(reservation.name), "total_amount"), 260763)

    def test_breakfast_and_commission_stay_out_of_the_row(self):
        reservation, folio = self.charge_folio(taxes=())
        add_transaction(folio, "Breakfast Charge", "Debit", 72727, NIGHT)
        add_transaction(folio, "Commision", "Debit", 10000, NIGHT)

        row = self.only_row(reservation.name)

        self.assertEqual(cell(row, "bf_revenue"), 0)
        self.assertEqual(cell(row, "comission"), 0)
        self.assertEqual(cell(row, "total_amount"), 260763)

    def test_posting_date_carries_the_charge_posting_not_the_payments(self):
        reservation, folio = self.charge_folio(taxes=())
        add_transaction(folio, "Payment", "Credit", 260763, DEPARTURE,
                        mode_of_payment="CASH")
        set_creation(folio, "Room Charge", NIGHT + " 00:43:20")
        charge_created = set_creation(folio, "Payment", DEPARTURE + " 14:49:43")

        posting = cell(self.only_row(reservation.name), "posting_date")

        # Exactly the charge's own posting, when the night audit posted it.
        # Asserting equality rather than "the payment is absent" is deliberate:
        # it fails both if the payment's timestamp were appended and if the
        # date were taken from the payment instead of the charge.
        self.assertEqual(posting, NIGHT + " 00:43:20")
        self.assertNotIn(str(charge_created), posting)

    def test_paid_date_scans_the_whole_folio(self):
        reservation, folio = self.charge_folio(taxes=())
        add_transaction(folio, "Payment", "Credit", 260763, DEPARTURE,
                        mode_of_payment="CASH")
        set_creation(folio, "Payment", DEPARTURE + " 14:49:43")

        self.assertEqual(cell(self.only_row(reservation.name), "paid_date"), DEPARTURE)

    def test_paid_date_is_blank_when_nothing_was_paid(self):
        reservation, _ = self.charge_folio(taxes=())

        self.assertEqual(cell(self.only_row(reservation.name), "paid_date"), "")


class TestTransferredCharges(FrappeTestCase):
    """A stay whose charges were transferred away still produces a row.

    ``transfer_to_another_folio()`` re-parents the rows onto the receiving folio
    and leaves a ``Transferred from ...`` remark behind, so the original folio
    holds nothing at all. That is how group bookings work on Jakarta: RSV-26707's
    six rows sit on F-27548 and its own folio F-27547 is empty.

    The stay must still appear, with zeros, because that zero is how the desk
    sees that the charge went elsewhere. Omitting the row hides the stay
    entirely, which is worse than showing it with no money.
    """

    def rows_for(self, name, on_date=NIGHT):
        return [row for row in audit_report.get_data_detail(on_date, 1)
                if rsv(row) == name]

    def transferred_stay(self):
        reservation = make_reservation()
        own_folio = make_folio(reservation=reservation.name)
        receiving = make_folio(folio_type="Master")
        add_transaction(
            receiving, "Room Charge", "Debit", 260763, NIGHT, actual_room_rate=376953,
            remark="Transferred from {0} to Folio {1}".format(
                own_folio.name, receiving.name))
        return reservation, own_folio, receiving

    def test_a_transferred_stay_still_appears_with_zero_money(self):
        reservation, _, _ = self.transferred_stay()

        rows = self.rows_for(reservation.name)

        self.assertEqual(len(rows), 1, "the stay must stay visible")
        self.assertEqual(cell(rows[0], "actual_room_rate"), 0)
        self.assertEqual(cell(rows[0], "actual_room_nett"), 0)
        self.assertEqual(cell(rows[0], "total_amount"), 0)

    def test_the_receiving_folio_keeps_the_money(self):
        _, _, receiving = self.transferred_stay()

        received = self.rows_for(receiving.name)

        self.assertEqual(len(received), 1)
        self.assertEqual(cell(received[0], "total_amount"), 260763)

    def test_a_charged_stay_is_not_emitted_twice(self):
        reservation = make_reservation()
        folio = make_folio(reservation=reservation.name)
        add_transaction(folio, "Room Charge", "Debit", 260763, NIGHT)

        self.assertEqual(len(self.rows_for(reservation.name)), 1)

    def test_a_no_show_is_not_an_occupant(self):
        """Occupancy is not status-only: the stay must cover the night."""
        reservation = make_reservation(status="No Show", arrival=NIGHT,
                                       departure=DEPARTURE)
        make_folio(reservation=reservation.name)

        self.assertEqual(self.rows_for(reservation.name), [])
