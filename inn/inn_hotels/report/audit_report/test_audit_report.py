# -*- coding: utf-8 -*-
# Copyright (c) 2025, PMM and Contributors
# See license.txt
"""Tests for the Audit Report's row shaping and money aggregation.

These cover the regressions that made the report disagree with the finance
spreadsheet, so they are deliberately DB-free: the classification rules are
passed in rather than read from Inn Hotels Setting.

The money tests are written against *posted transactions*, which are the
report's source of truth for a business date. ``Inn Reservation``'s
``nett_actual_room_rate`` is deliberately NOT used: it is a single current
scalar rather than a per-night history, and it disagrees with what was posted
on roughly 15% of room-charge folio-days. That divergence is tracked
separately -- do not "simplify" this report back to the reservation field.
"""

from __future__ import unicode_literals

import datetime
import unittest
from unittest import mock

import frappe

from inn.helper.revenue_lines import (
    LINE_BREAKFAST,
    LINE_COMMISSION,
    LINE_OTHER,
    LINE_REFUND,
    LINE_ROOM,
)
from inn.inn_hotels.report.audit_report import audit_report

LINE_MAP = {
    "Room Charge": LINE_ROOM,
    "Room Charge Tax/Service": LINE_ROOM,
    "Breakfast Charge": LINE_BREAKFAST,
    "Breakfast Charge Tax/Service": LINE_BREAKFAST,
    "Commision": LINE_COMMISSION,
    "Refund": LINE_REFUND,
    "Restaurant Food": LINE_OTHER,
}

#: The types carrying the tax/service element of a revenue line. Mirrors
#: ``get_tax_service_types()`` for the shipped transaction type names, and is
#: passed in rather than read from ``Inn Hotels Setting`` to keep these tests
#: DB-free.
TAX_SERVICE_TYPES = {"Room Charge Tax/Service", "Breakfast Charge Tax/Service"}

ON_DATE = "2025-11-05"


def column_index(fieldname):
    """Position of ``fieldname`` in the emitted columns.

    Resolved by name rather than hard-coded: the report's column order is an
    interface, so a test that pins raw slot numbers has to be rewritten every
    time a column is inserted -- which is exactly how a real misalignment would
    slip through unnoticed.
    """
    fieldnames = [column["fieldname"] for column in audit_report.get_columns()]
    return fieldnames.index(fieldname)


def row_value(row, fieldname):
    return row[column_index(fieldname)]


def trx(transaction_type, flag, amount, mode=None, rate=None, creation=None):
    return frappe._dict(
        parent="F-1",
        transaction_type=transaction_type,
        flag=flag,
        amount=amount,
        mode_of_payment=mode,
        actual_room_rate=rate,
        creation=creation,
    )


class TestAuditReportColumns(unittest.TestCase):
    def test_column_count_and_audit_date_appended(self):
        columns = audit_report.get_columns()
        self.assertEqual(len(columns), 17)
        self.assertEqual(columns[0]["fieldname"], "rsv")
        self.assertEqual(columns[-1]["fieldname"], "audit_date")
        for column in columns:
            self.assertTrue(column.get("label"), column)

    def test_original_column_order_preserved(self):
        """The finance team's paste depends on these positions.

        ``bed_type`` is deliberately inserted after ``room_type``, so the
        original columns keep their names but everything from ``actual_room``
        onward moves one slot right. ``audit_date`` stays last.
        """
        fieldnames = [column["fieldname"] for column in audit_report.get_columns()]
        self.assertEqual(
            fieldnames,
            [
                "rsv",
                "customer",
                "room_type",
                "bed_type",
                "actual_room",
                "actual_room_rate",
                "actual_room_nett",
                "bf_revenue",
                "comission",
                "payment_by",
                "status",
                "mode_of_payment",
                "total_amount",
                "posting_date",
                "paid_date",
                "remark",
                "audit_date",
            ],
        )

    def test_bed_type_sits_directly_after_room_type(self):
        self.assertEqual(
            column_index("bed_type"), column_index("room_type") + 1
        )
        columns = audit_report.get_columns()
        self.assertEqual(columns[column_index("bed_type")]["label"], "Bed Type")


class TestAuditReportAggregation(unittest.TestCase):
    def test_credits_no_longer_feed_total_amount(self):
        """Total Amount is the gross room charge now, not the day's collections.

        The regression this replaces -- Down Payment / Payment / Deposit being
        dropped from the old collections total -- still matters, but it belongs
        to Front Desk Daily Revenue Recap, which owns collections. Here the
        money columns describe the room charge.
        """
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Payment", "Credit", 100, mode="Cash"),
                trx("Down Payment", "Credit", 200, mode="BCA TRANSFER"),
                trx("Payment", "Credit", 300, mode="BCA EDC"),
                trx("Deposit", "Credit", 400, mode="Voucher"),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["total_amount"], 0)

    def test_total_amount_is_the_gross_room_charge(self):
        """Gross: the charge plus its posted tax/service. Nothing else."""
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge", "Debit", 500),
                trx("Room Charge Tax/Service", "Debit", 105),
                trx("Refund", "Debit", 50),
                trx("Room Payment", "Credit", 500, mode="Cash"),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["total_amount"], 605)
        self.assertEqual(detail["F-1"]["actual_room_nett"], 500)

    def test_gross_is_nett_plus_the_posted_tax_service(self):
        """The relationship the two columns must hold, on RSV-26612's numbers.

        Jakarta posted 260,763 nett with two tax/service rows of 26,076 and
        28,685 -- 315,524 gross, which is exactly the reservation's rate.
        """
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge", "Debit", 260763),
                trx("Room Charge Tax/Service", "Debit", 26076),
                trx("Room Charge Tax/Service", "Debit", 28685),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        bucket = detail["F-1"]
        self.assertEqual(bucket["actual_room_nett"], 260763)
        self.assertEqual(bucket["total_amount"], 315524)
        self.assertEqual(bucket["total_amount"], 260763 + 26076 + 28685)

    def test_refund_is_not_room_revenue(self):
        detail, _ = audit_report.summarise_transactions([trx("Refund", "Debit", 75)], LINE_MAP, TAX_SERVICE_TYPES)
        self.assertEqual(detail["F-1"]["actual_room_nett"], 0)
        self.assertEqual(detail["F-1"]["breakfast_revenue"], 0)

    def test_summarise_transactions_no_longer_collects_modes(self):
        """Modes are a folio-level fact, resolved by get_payment_modes().

        Collecting them here would scope them to one date's transactions, which
        is the defect this change removes: RSV-26612's QRIS payment was audited
        on the departure date, so a date-scoped column could never show it on
        the night's row. The old test here guarded the `[:-2]` string slicing
        that produced 'BY BCA EDC 550.000, '; that guard now lives in
        TestPaymentModes.
        """
        detail, _ = audit_report.summarise_transactions(
            [trx("Room Payment", "Credit", 100, mode="BCA EDC")], LINE_MAP, TAX_SERVICE_TYPES
        )
        self.assertNotIn("modes", detail["F-1"])

    def test_tax_service_lines_are_excluded_from_room_nett(self):
        """The column is named "Actual Room Nett", so it must hold the nett.

        Regression: the v1.2.0 refactor replaced exact transaction-type matching
        with line classification, which folded "Room Charge Tax/Service" into the
        same bucket as "Room Charge" and silently turned the column into gross.
        """
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge", "Debit", 500),
                trx("Room Charge Tax/Service", "Debit", 105),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["actual_room_nett"], 500)

    def test_breakfast_no_longer_reaches_a_column(self):
        """Breakfast is out of scope: the recap carries it, with the tax split.

        The old test here pinned that breakfast tax/service was excluded from
        the breakfast nett. Nothing accumulates breakfast now, so there is no
        nett to exclude it from -- the assertion that matters is that a
        breakfast posting stays out of the room columns too.
        """
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge", "Debit", 500),
                trx("Breakfast Charge", "Debit", 80),
                trx("Breakfast Charge Tax/Service", "Debit", 16.8),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["breakfast_revenue"], 0)
        self.assertEqual(detail["F-1"]["total_amount"], 500)

    def test_room_nett_follows_the_posted_rows_not_a_tax_rate(self):
        """The tax/service split is not a fixed 21%.

        ``Inn Tax`` can be "No Service Tax", "Service Only (10%)" or "Service
        Tax 21%", and a reservation can post only one of that tax's breakdown
        rows. Measured on a live folio: 409,090 nett with a single 40,910 tax
        row (10%) -- not the 371,899 a 21% split would imply, which is what the
        reservation's own ``nett_actual_room_rate`` holds.

        A fix that subtracted a fixed percentage of the gross would pass the
        21% case above and fail here, so this pins that the report subtracts
        the *posted* tax/service rows rather than applying any rate.
        """
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge", "Debit", 409090),
                trx("Room Charge Tax/Service", "Debit", 40910),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["actual_room_nett"], 409090)

    def test_tax_only_folio_reports_zero_nett(self):
        """No nett row posted means no nett revenue, however large the tax."""
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge Tax/Service", "Debit", 121),
                trx("Breakfast Charge Tax/Service", "Debit", 21),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["actual_room_nett"], 0)
        self.assertEqual(detail["F-1"]["breakfast_revenue"], 0)
        # Gross still reflects the posted *room* tax. The breakfast tax is not a
        # room line, so it stays out of the room columns entirely.
        self.assertEqual(detail["F-1"]["total_amount"], 121)

    def test_tax_service_lines_are_still_recognised(self):
        """The replaced test's real intent, kept.

        Tax/service rows have a known revenue line, so excluding them from the
        nett columns must not cause them to be reported as unmapped.
        """
        _, unmapped = audit_report.summarise_transactions(
            [
                trx("Room Charge Tax/Service", "Debit", 121),
                trx("Breakfast Charge Tax/Service", "Debit", 21),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(unmapped, [])

    def test_tax_service_is_part_of_gross_but_not_of_nett(self):
        """The two columns must differ by exactly the posted tax/service.

        The old test here asserted the tax did *not* reach Total Amount, because
        Total Amount was the day's collections and a charge is not a collection.
        It is the gross room charge now, so the tax belongs in it -- and the
        nett column must still exclude it.
        """
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge", "Debit", 500),
                trx("Room Charge Tax/Service", "Debit", 105),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["actual_room_nett"], 500)
        self.assertEqual(detail["F-1"]["total_amount"], 605)

    def test_unrecognised_tax_service_type_is_reported(self):
        """A tax type outside the configured set would silently inflate nett.

        ``get_tax_service_types()`` recognises the shipped names plus
        ``<configured revenue type> Tax/Service``. A type that feeds a nett
        column and carries a tax/service name but is not in that set is
        reported, rather than quietly counted as revenue.
        """
        line_map = dict(LINE_MAP, **{"Room Tax/Service": LINE_ROOM})
        suspects = audit_report.unrecognised_tax_service_types(
            [
                trx("Room Charge", "Debit", 500),
                trx("Room Tax/Service", "Debit", 105),
                trx("Room Charge Tax/Service", "Debit", 105),
                trx("Room Payment", "Credit", 500, mode="Cash"),
            ],
            TAX_SERVICE_TYPES,
            line_map,
        )
        self.assertEqual(suspects, ["Room Tax/Service"])

    def test_no_suspects_when_every_tax_service_type_is_configured(self):
        suspects = audit_report.unrecognised_tax_service_types(
            [
                trx("Room Charge", "Debit", 500),
                trx("Room Charge Tax/Service", "Debit", 105),
                trx("Breakfast Charge", "Debit", 80),
                trx("Breakfast Charge Tax/Service", "Debit", 16.8),
                trx("Restaurant Food", "Debit", 40),
            ],
            TAX_SERVICE_TYPES,
            LINE_MAP,
        )
        self.assertEqual(suspects, [])

    def test_actual_room_rate_is_taken_from_the_room_charge(self):
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge", "Debit", 500, rate=550000),
                trx("Room Charge", "Debit", 500, rate=None),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["actual_room_rate"], 550000)

    def test_commission_no_longer_reaches_a_column(self):
        detail, _ = audit_report.summarise_transactions(
            [trx("Commision", "Debit", 90)], LINE_MAP, TAX_SERVICE_TYPES
        )
        self.assertEqual(detail["F-1"]["comission"], 0)
        self.assertEqual(detail["F-1"]["total_amount"], 0)

    def test_unmapped_type_is_reported(self):
        _, unmapped = audit_report.summarise_transactions(
            [trx("Brand New Charge", "Debit", 10)], LINE_MAP, TAX_SERVICE_TYPES
        )
        self.assertEqual(unmapped, ["Brand New Charge"])

    def test_payment_types_are_not_reported_as_unmapped(self):
        """Regression: credits have no revenue line by design.

        Classifying them produced a warning on every run naming Room Payment,
        Down Payment, Payment and Deposit as unmapped.
        """
        _, unmapped = audit_report.summarise_transactions(
            [
                trx("Room Payment", "Credit", 100, mode="Cash"),
                trx("Down Payment", "Credit", 100, mode="Cash"),
                trx("Payment", "Credit", 100, mode="Cash"),
                trx("Deposit", "Credit", 100, mode="Cash"),
                trx("Room Charge", "Debit", 400),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(unmapped, [])

    def test_a_credit_alone_yields_no_money_and_no_warning(self):
        """Credits bypass classification: they fund no column and warn nobody.

        Replaces the old "collections are still counted" test. Collections are
        no longer this report's business, but the classification bypass still
        is -- classifying a payment type would warn on every run.
        """
        detail, unmapped = audit_report.summarise_transactions(
            [trx("Room Payment", "Credit", 250, mode="BCA EDC")], LINE_MAP, TAX_SERVICE_TYPES
        )
        self.assertEqual(detail["F-1"]["total_amount"], 0)
        self.assertEqual(unmapped, [])


class TestPaymentModes(unittest.TestCase):
    """Mode of Payment is the guest's whole payment history, in payment order.

    Deliberately not date-scoped: the column answers "how did this guest pay",
    so a payment audited after the report date -- a QRIS settlement at checkout,
    an advance deposit taken before arrival -- still describes the folio's row.
    """

    def modes_from(self, rows, folios=("F-1",)):
        with mock.patch.object(frappe.db, "sql", return_value=rows) as sql:
            return audit_report.get_payment_modes(list(folios)), sql.call_args

    def payment(self, folio, mode, creation):
        return frappe._dict(parent=folio, mode_of_payment=mode, creation=creation)

    def test_modes_are_ordered_by_first_payment(self):
        modes, _ = self.modes_from([
            self.payment("F-1", "QRIS", "2026-11-08 14:49:43"),
            self.payment("F-1", "CASH", "2026-11-08 15:10:00"),
        ])
        # Chronological, not alphabetical: the guest paid QRIS first.
        self.assertEqual(modes["F-1"], ["QRIS", "CASH"])

    def test_a_repeated_mode_appears_once(self):
        modes, _ = self.modes_from([
            self.payment("F-1", "Cash", "2026-11-07 09:00:00"),
            self.payment("F-1", "Cash", "2026-11-09 09:00:00"),
        ])
        self.assertEqual(modes["F-1"], ["Cash"])

    def test_modes_are_grouped_per_folio(self):
        modes, _ = self.modes_from([
            self.payment("F-1", "Cash", "2026-11-07 09:00:00"),
            self.payment("F-2", "QRIS", "2026-11-07 10:00:00"),
        ], folios=("F-1", "F-2"))
        self.assertEqual(modes["F-1"], ["Cash"])
        self.assertEqual(modes["F-2"], ["QRIS"])

    def test_the_query_is_not_date_scoped(self):
        """A payment on another date must still describe this folio's row."""
        _, call = self.modes_from([])
        self.assertNotIn("audit_date", call[0][0])

    def test_the_query_takes_only_non_void_credits(self):
        _, call = self.modes_from([])
        query = call[0][0].lower()
        self.assertIn("is_void = 0", query)
        self.assertIn("credit", query)
        self.assertIn("flag", query)

    def test_no_folios_means_no_query(self):
        with mock.patch.object(frappe.db, "sql") as sql:
            self.assertEqual(audit_report.get_payment_modes([]), {})
        sql.assert_not_called()


class TestPaidDates(unittest.TestCase):
    """Paid Date is a **business** date, so it comes from ``audit_date``.

    Not the wall clock. A payment taken at 00:56 during the night audit is
    created on one calendar day and audited to the one before; the finance
    spreadsheet is organised by business date, so that is what the column must
    carry. RSV-26612's City Ledger payment is exactly this case: created
    2026-11-08 00:56, audited to 2026-11-07.
    """

    def paid_from(self, rows):
        with mock.patch.object(frappe.db, "sql", return_value=list(rows)) as sql:
            return audit_report.get_paid_dates(["F-1"]), sql.call_args

    def test_the_earliest_business_date_is_returned(self):
        result, _ = self.paid_from([
            frappe._dict(parent="F-1", first_payment=datetime.date(2026, 11, 7)),
        ])
        self.assertEqual(result["F-1"], "2026-11-07")

    def test_the_query_dates_payments_by_audit_date_not_creation(self):
        _, call = self.paid_from([])
        query = call[0][0].lower()

        self.assertIn("min(audit_date)", query)
        self.assertNotIn("creation", query)

    def test_a_folio_with_no_payment_is_absent(self):
        result, _ = self.paid_from([frappe._dict(parent="F-1", first_payment=None)])
        self.assertEqual(result, {})

    def test_no_folios_means_no_query(self):
        with mock.patch.object(frappe.db, "sql") as sql:
            self.assertEqual(audit_report.get_paid_dates([]), {})
        sql.assert_not_called()


class TestAuditReportRowShaping(unittest.TestCase):
    def setUp(self):
        self.detail = audit_report.empty_detail()
        self.detail["total_amount"] = 150
        self.detail["actual_room_nett"] = 500
        self.detail["actual_room_rate"] = 550000

    def test_row_matches_column_count(self):
        row = audit_report.build_row(
            frappe._dict(
                name="RSV-1",
                customer_id="CUST-1",
                room_type="Deluxe",
                actual_room_id="R-101",
                channel="Walk In",
                bill_instructions="note",
            ),
            self.detail,
            audit_report.STATUS_PAID,
            1,
            ON_DATE,
        )
        self.assertEqual(len(row), len(audit_report.get_columns()))

    def test_posting_date_carries_the_room_charge_postings(self):
        import datetime

        self.detail["room_postings"] = [
            datetime.datetime(2025, 11, 6, 0, 43, 20),
            datetime.datetime(2025, 11, 6, 0, 43, 21),
        ]
        row = audit_report.build_row(
            frappe._dict(name="RSV-1", customer_id="C", room_type="", bed_type="",
                         actual_room_id="", channel="", bill_instructions=None),
            self.detail, audit_report.STATUS_PAID, 1, ON_DATE,
        )
        self.assertEqual(row_value(row, "audit_date"), ON_DATE)
        # The charge's own posting, oldest first -- not the payments'.
        self.assertTrue(row_value(row, "posting_date").startswith("2025-11-06 00:43:20"))

    def test_paid_date_comes_from_the_folio_not_the_date(self):
        """Paid Date spans the folio, so it is passed in, not derived from detail."""
        row = audit_report.build_row(
            frappe._dict(name="RSV-1", customer_id="C", room_type="", bed_type="",
                         actual_room_id="", channel="", bill_instructions=None),
            self.detail, audit_report.STATUS_PAID, 1, ON_DATE,
            paid_date="2025-11-09",
        )
        self.assertEqual(row_value(row, "paid_date"), "2025-11-09")

    def test_paid_date_is_blank_when_the_folio_has_not_paid(self):
        row = audit_report.build_row(
            frappe._dict(name="RSV-1", customer_id="C", room_type="", bed_type="",
                         actual_room_id="", channel="", bill_instructions=None),
            self.detail, audit_report.STATUS_UNPAID, 1, ON_DATE,
        )
        self.assertEqual(row_value(row, "paid_date"), "")

    def test_bed_type_is_emitted_for_a_reservation(self):
        row = audit_report.build_row(
            frappe._dict(name="RSV-1", customer_id="C", room_type="Deluxe",
                         bed_type="Queen", actual_room_id="R-101", channel="",
                         bill_instructions=None),
            self.detail, audit_report.STATUS_PAID, 1, ON_DATE,
        )
        self.assertEqual(row_value(row, "bed_type"), "Queen")
        self.assertEqual(row_value(row, "room_type"), "Deluxe")

    def test_mode_suppressed_when_filter_off(self):
        row = audit_report.build_row(
            frappe._dict(name="RSV-1", customer_id="C", room_type="", bed_type="",
                         actual_room_id="", channel="", bill_instructions=None),
            self.detail, audit_report.STATUS_PAID, 0, ON_DATE,
            modes=["Cash", "QRIS"],
        )
        self.assertEqual(row_value(row, "mode_of_payment"), "")

    def test_mode_of_payment_joins_in_payment_order(self):
        """Order is the guest's, not the alphabet's.

        RSV-26612 on Jakarta paid by City Ledger for the room and BCA QRIS at
        checkout. Sorted, that reads "BCA QRIS, City Ledger" -- which misstates
        which payment came first.
        """
        row = audit_report.build_row(
            frappe._dict(name="RSV-1", customer_id="C", room_type="", bed_type="",
                         actual_room_id="", channel="", bill_instructions=None),
            self.detail, audit_report.STATUS_PAID, 1, ON_DATE,
            modes=["City Ledger", "BCA QRIS"],
        )
        self.assertEqual(row_value(row, "mode_of_payment"), "City Ledger, BCA QRIS")

    def test_mode_of_payment_is_empty_when_no_folio_paid(self):
        row = audit_report.build_row(
            frappe._dict(name="RSV-1", customer_id="C", room_type="", bed_type="",
                         actual_room_id="", channel="", bill_instructions=None),
            self.detail, audit_report.STATUS_PAID, 1, ON_DATE,
        )
        self.assertEqual(row_value(row, "mode_of_payment"), "")

    def test_a_folio_without_a_reservation_keeps_the_contract_and_names_the_folio(self):
        source = audit_report.resolve_folio_row(frappe._dict(
            folio="F-16280", folio_type="Desk", folio_customer_id=None,
            folio_channel=None, folio_bill_instructions="misc",
            reservation=None, customer_id=None, room_type=None, bed_type=None,
            actual_room_id=None, channel=None, bill_instructions=None,
        ))
        row = audit_report.build_row(
            source, self.detail, audit_report.STATUS_PAID, 1, ON_DATE,
        )
        self.assertEqual(len(row), len(audit_report.get_columns()))
        self.assertEqual(row_value(row, "rsv"), "F-16280")
        # A folio with no reservation has no bed type, exactly as it has no room type.
        self.assertEqual(row_value(row, "bed_type"), "")
        self.assertEqual(row_value(row, "room_type"), "")
        self.assertIn("Desk folio F-16280", row_value(row, "remark"))
        self.assertIn("misc", row_value(row, "remark"))

    def test_reservation_with_no_folio_yields_zero_row(self):
        """A reservation with no folio and blank status: zeros, not an error."""
        row = audit_report.build_row(
            frappe._dict(name="RSV-NOFOLIO", customer_id="CUST-1", room_type="Deluxe",
                         bed_type="King", actual_room_id="R-101", channel="Walk In",
                         bill_instructions=None),
            audit_report.empty_detail(), "", 1, ON_DATE,
        )
        self.assertEqual(len(row), len(audit_report.get_columns()))
        self.assertEqual(row_value(row, "rsv"), "RSV-NOFOLIO")
        self.assertEqual(row_value(row, "bed_type"), "King")
        for fieldname in ("actual_room_rate", "actual_room_nett", "bf_revenue",
                          "comission", "total_amount"):
            self.assertEqual(row_value(row, fieldname), 0,
                             "%s should be zero" % fieldname)
        self.assertEqual(row_value(row, "status"), "")
        self.assertEqual(row_value(row, "mode_of_payment"), "")
        self.assertEqual(row_value(row, "posting_date"), "")
        self.assertEqual(row_value(row, "paid_date"), "")
        self.assertEqual(row_value(row, "audit_date"), ON_DATE)


class TestAuditReportFolioDrivenRows(unittest.TestCase):
    """Rows come from charged folios and occupied rooms, and from nothing else.

    Two sources, deliberately:

    * folios charged room revenue on the date -- the ledger;
    * reservations that occupied a room that night but hold no charge of their
      own, because ``transfer_to_another_folio()`` re-parented it to another
      folio. A group booking's member rooms are all like this, and the desk
      reads the zero on such a row as "the charge went elsewhere".

    What is gone is the **activity** branch: a stay is no longer re-added on its
    departure date merely because a late-checkout fee was posted that day.
    """

    def folio_row(self, folio, name=None):
        return frappe._dict(
            folio=folio, name=name or folio, customer_id="CUST-1",
            room_type="", bed_type="", actual_room_id="", channel="Walk In",
            bill_instructions=None,
        )

    def rows_for(self, charged=(), occupants=(), transactions=None,
                 modes=None, settlement=None, paid_dates=None):
        with mock.patch.object(audit_report, "get_folio_rows", return_value=list(charged)), \
                mock.patch.object(audit_report, "get_occupancy_rows",
                                  return_value=list(occupants)), \
                mock.patch.object(audit_report, "get_transactions_for_date",
                                  return_value=transactions or []), \
                mock.patch.object(audit_report, "get_settlement_status",
                                  return_value=settlement or {}), \
                mock.patch.object(audit_report, "get_payment_modes",
                                  return_value=modes or {}), \
                mock.patch.object(audit_report, "get_paid_dates",
                                  return_value=paid_dates or {}):
            return audit_report.get_data_detail(ON_DATE, 1)

    def test_nothing_charged_and_nobody_in_house_produces_no_rows(self):
        self.assertEqual(self.rows_for(), [])

    def test_one_row_is_emitted_per_charged_folio(self):
        out = self.rows_for(charged=[self.folio_row("F-1", "RSV-1"),
                                     self.folio_row("F-2", "RSV-2")])

        self.assertEqual(len(out), 2)
        self.assertEqual([row_value(row, "rsv") for row in out], ["RSV-1", "RSV-2"])
        for row in out:
            self.assertEqual(len(row), len(audit_report.get_columns()))
            self.assertEqual(row_value(row, "audit_date"), ON_DATE)

    def test_an_occupant_holding_no_charge_is_appended(self):
        out = self.rows_for(charged=[self.folio_row("F-1", "RSV-1")],
                            occupants=[self.folio_row("F-2", "RSV-2")])

        self.assertEqual([row_value(row, "rsv") for row in out], ["RSV-1", "RSV-2"])
        self.assertEqual(row_value(out[1], "total_amount"), 0)
        self.assertEqual(row_value(out[1], "actual_room_rate"), 0)

    def test_occupancy_selection_is_told_which_folios_were_charged(self):
        with mock.patch.object(audit_report, "get_folio_rows",
                               return_value=[self.folio_row("F-1")]), \
                mock.patch.object(audit_report, "get_occupancy_rows",
                                  return_value=[]) as occupants, \
                mock.patch.object(audit_report, "get_transactions_for_date", return_value=[]), \
                mock.patch.object(audit_report, "get_settlement_status", return_value={}), \
                mock.patch.object(audit_report, "get_payment_modes", return_value={}), \
                mock.patch.object(audit_report, "get_paid_dates", return_value={}):
            audit_report.get_data_detail(ON_DATE, 1)

        self.assertEqual(occupants.call_args[0][1], {"F-1"})

    def test_every_selected_folio_is_queried_for_its_transactions(self):
        with mock.patch.object(audit_report, "get_folio_rows",
                               return_value=[self.folio_row("F-1"), self.folio_row("F-2")]), \
                mock.patch.object(audit_report, "get_occupancy_rows", return_value=[]), \
                mock.patch.object(audit_report, "get_transactions_for_date",
                                  return_value=[]) as transactions, \
                mock.patch.object(audit_report, "get_settlement_status", return_value={}), \
                mock.patch.object(audit_report, "get_payment_modes", return_value={}), \
                mock.patch.object(audit_report, "get_paid_dates", return_value={}):
            audit_report.get_data_detail(ON_DATE, 1)

        self.assertEqual(sorted(transactions.call_args[0][0]), ["F-1", "F-2"])
        self.assertEqual(transactions.call_args[0][1], ON_DATE)

    def test_a_charged_folio_is_still_queried_when_it_holds_an_occupant(self):
        """The charged row must not be lost to the occupancy pass."""
        with mock.patch.object(audit_report, "get_folio_rows",
                               return_value=[self.folio_row("F-1", "RSV-1")]), \
                mock.patch.object(audit_report, "get_occupancy_rows", return_value=[]), \
                mock.patch.object(audit_report, "get_transactions_for_date",
                                  return_value=[]) as transactions, \
                mock.patch.object(audit_report, "get_settlement_status", return_value={}), \
                mock.patch.object(audit_report, "get_payment_modes", return_value={}), \
                mock.patch.object(audit_report, "get_paid_dates", return_value={}):
            audit_report.get_data_detail(ON_DATE, 1)

        self.assertEqual(transactions.call_args[0][0], ["F-1"])

    def test_the_folios_payment_modes_reach_the_row(self):
        out = self.rows_for(charged=[self.folio_row("F-1", "RSV-1")],
                            modes={"F-1": ["City Ledger", "BCA QRIS"]})

        self.assertEqual(row_value(out[0], "mode_of_payment"), "City Ledger, BCA QRIS")


class TestOccupancySelection(unittest.TestCase):
    """The predicate for stays that hold no charge of their own."""

    def selected(self, rows, charged=frozenset()):
        with mock.patch.object(frappe.db, "sql", return_value=rows) as sql:
            return audit_report.get_occupancy_rows(ON_DATE, set(charged)), sql.call_args

    def occupant(self, folio, name):
        return frappe._dict(folio=folio, name=name, customer_id="CUST-1",
                            room_type="Superior", bed_type="Double",
                            actual_room_id="R-605", channel="TIKET.COM",
                            bill_instructions=None)

    def test_an_occupant_holding_no_charge_is_selected(self):
        out, _ = self.selected([self.occupant("F-1", "RSV-1")])
        self.assertEqual([row.name for row in out], ["RSV-1"])

    def test_an_occupant_whose_folio_was_charged_is_not_repeated(self):
        out, _ = self.selected([self.occupant("F-1", "RSV-1")], charged={"F-1"})
        self.assertEqual(out, [])

    def test_the_query_selects_on_occupancy_not_activity(self):
        """The activity branch is what produced the phantom rows. It stays gone."""
        _, call = self.selected([])
        query, params = call[0]

        self.assertNotIn("Inn Folio Transaction", query)
        self.assertIn("expected_departure > %s", query)
        self.assertIn(audit_report.STATUS_IN_HOUSE, params)
        self.assertIn(audit_report.STATUS_FINISH, params)


class TestRoomLineTypes(unittest.TestCase):
    """The selection type set is derived from the line map, never hardcoded."""

    def test_room_types_come_from_the_line_map(self):
        self.assertEqual(
            audit_report.get_room_line_types(LINE_MAP),
            {"Room Charge", "Room Charge Tax/Service"},
        )

    def test_a_renamed_configured_revenue_type_is_included(self):
        line_map = dict(LINE_MAP)
        line_map["Accommodation"] = LINE_ROOM
        self.assertIn("Accommodation", audit_report.get_room_line_types(line_map))

    def test_non_room_lines_are_excluded(self):
        types = audit_report.get_room_line_types(LINE_MAP)
        for name in ("Breakfast Charge", "Commision", "Restaurant Food"):
            self.assertNotIn(name, types, "%s must not select a room charge row" % name)

    def test_an_empty_line_map_selects_nothing(self):
        self.assertEqual(audit_report.get_room_line_types({}), set())


class TestFolioRowResolution(unittest.TestCase):
    """One selected folio becomes one report row source."""

    def test_a_folio_with_a_reservation_renders_the_reservation(self):
        row = audit_report.resolve_folio_row(frappe._dict(
            folio="F-1", folio_type="Guest", folio_customer_id="FOLIO-CUST",
            folio_channel="Direct", folio_bill_instructions="note",
            reservation="RSV-1", customer_id="CUST-1", room_type="Superior",
            bed_type="Double", actual_room_id="R-620", channel="TIKET.COM",
        ))

        self.assertEqual(row.name, "RSV-1")
        self.assertEqual(row.folio, "F-1")
        self.assertEqual(row.customer_id, "CUST-1")
        self.assertEqual(row.actual_room_id, "R-620")
        self.assertEqual(row.channel, "TIKET.COM")
        # Inn Reservation has no bill_instructions; the remark is the folio's.
        self.assertEqual(row.bill_instructions, "note")

    def test_a_folio_without_a_reservation_names_itself_and_its_type(self):
        row = audit_report.resolve_folio_row(frappe._dict(
            folio="F-27433", folio_type="Master", folio_customer_id="IBU GEDE KARIM",
            folio_channel="Direct", folio_bill_instructions=None,
            reservation=None, customer_id=None, room_type=None, bed_type=None,
            actual_room_id=None, channel=None, bill_instructions=None,
        ))

        self.assertEqual(row.name, "F-27433")
        self.assertEqual(row.customer_id, "IBU GEDE KARIM")
        self.assertEqual(row.channel, "Direct")
        self.assertEqual(row.room_type, "")
        self.assertEqual(row.bed_type, "")
        self.assertEqual(row.actual_room_id, "")
        self.assertEqual(row.bill_instructions, "Master folio F-27433")

    def test_a_folio_with_no_type_still_names_itself(self):
        row = audit_report.resolve_folio_row(frappe._dict(
            folio="F-9", folio_type=None, folio_customer_id=None,
            folio_channel=None, folio_bill_instructions=None,
            reservation=None, customer_id=None, room_type=None, bed_type=None,
            actual_room_id=None, channel=None, bill_instructions=None,
        ))

        self.assertEqual(row.bill_instructions, "Non-guest folio F-9")

    def test_folio_instructions_are_appended_to_the_remark(self):
        row = audit_report.resolve_folio_row(frappe._dict(
            folio="F-9", folio_type="Desk", folio_customer_id=None,
            folio_channel=None, folio_bill_instructions="misc",
            reservation=None, customer_id=None, room_type=None, bed_type=None,
            actual_room_id=None, channel=None, bill_instructions=None,
        ))

        self.assertEqual(row.bill_instructions, "Desk folio F-9: misc")


class TestSelectionQuery(unittest.TestCase):
    """The predicate's three conditions, guarded at the SQL level.

    A mocked test cannot prove which folios the query returns -- that is
    test_audit_report_selection.py's job, against real records. It can prove the
    query still carries the conditions the predicate depends on, which is where
    a careless edit does its damage.
    """

    def captured_call(self, line_map=LINE_MAP, on_date=ON_DATE):
        with mock.patch.object(frappe.db, "sql", return_value=[]) as sql:
            audit_report.get_folio_rows(on_date, line_map)
        return sql.call_args

    def test_the_query_filters_on_the_report_date_and_non_void_rows(self):
        query, params = self.captured_call()[0]

        self.assertIn("trx.audit_date = %s", query)
        self.assertIn("trx.is_void = 0", query)
        self.assertIn(ON_DATE, params)

    def test_the_query_binds_every_room_line_type_and_no_other(self):
        _, params = self.captured_call()[0]
        bound = set(params)

        self.assertTrue({"Room Charge", "Room Charge Tax/Service"} <= bound, bound)
        self.assertNotIn("Breakfast Charge", bound)
        self.assertNotIn("Restaurant Food", bound)

    def test_no_room_line_types_selects_nothing_without_querying(self):
        with mock.patch.object(frappe.db, "sql") as sql:
            self.assertEqual(audit_report.get_folio_rows(ON_DATE, {}), [])

        sql.assert_not_called()
