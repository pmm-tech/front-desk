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
    def test_every_credit_counts_as_collection(self):
        """Regression: Down Payment / Payment / Deposit used to be dropped."""
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
        self.assertEqual(detail["F-1"]["total_amount"], 1000)

    def test_debits_are_not_collections(self):
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge", "Debit", 500),
                trx("Refund", "Debit", 50),
                trx("Room Payment", "Credit", 500, mode="Cash"),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["total_amount"], 500)
        self.assertEqual(detail["F-1"]["actual_room_nett"], 500)

    def test_refund_is_not_room_revenue(self):
        detail, _ = audit_report.summarise_transactions([trx("Refund", "Debit", 75)], LINE_MAP, TAX_SERVICE_TYPES)
        self.assertEqual(detail["F-1"]["actual_room_nett"], 0)
        self.assertEqual(detail["F-1"]["breakfast_revenue"], 0)

    def test_mode_of_payment_is_clean_and_deduplicated(self):
        """Regression: the old code emitted 'BY BCA EDC 550.000, ' with [:-2]."""
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Payment", "Credit", 100, mode="BCA EDC"),
                trx("Room Payment", "Credit", 100, mode="BCA EDC"),
                trx("Payment", "Credit", 100, mode="Cash"),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["modes"], {"BCA EDC", "Cash"})

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

    def test_tax_service_lines_are_excluded_from_breakfast_nett(self):
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Breakfast Charge", "Debit", 80),
                trx("Breakfast Charge Tax/Service", "Debit", 16.8),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["breakfast_revenue"], 80)

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

    def test_tax_service_is_not_counted_as_a_collection(self):
        """Excluding tax/service from nett must not leak it into Total Amount."""
        detail, _ = audit_report.summarise_transactions(
            [
                trx("Room Charge", "Debit", 500),
                trx("Room Charge Tax/Service", "Debit", 105),
            ],
            LINE_MAP,
            TAX_SERVICE_TYPES,
        )
        self.assertEqual(detail["F-1"]["total_amount"], 0)

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

    def test_commission_is_collected_separately(self):
        detail, _ = audit_report.summarise_transactions(
            [trx("Commision", "Debit", 90)], LINE_MAP, TAX_SERVICE_TYPES
        )
        self.assertEqual(detail["F-1"]["comission"], 90)
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

    def test_collections_still_counted_when_classification_skipped(self):
        """Credits bypass classification, so the credit branch must still run."""
        detail, _ = audit_report.summarise_transactions(
            [trx("Room Payment", "Credit", 250, mode="BCA EDC")], LINE_MAP, TAX_SERVICE_TYPES
        )
        self.assertEqual(detail["F-1"]["total_amount"], 250)
        self.assertEqual(detail["F-1"]["modes"], {"BCA EDC"})


class TestAuditReportRowShaping(unittest.TestCase):
    def setUp(self):
        self.detail = audit_report.empty_detail()
        self.detail["total_amount"] = 150
        self.detail["modes"] = {"Cash"}
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

    def test_audit_date_and_paid_date_are_populated(self):
        import datetime

        self.detail["creations"] = [
            datetime.datetime(2025, 11, 6, 0, 30),
            datetime.datetime(2025, 11, 6, 1, 45),
        ]
        row = audit_report.build_row(
            frappe._dict(name="RSV-1", customer_id="C", room_type="", actual_room_id="",
                         channel="", bill_instructions=None),
            self.detail, audit_report.STATUS_PAID, 1, ON_DATE,
        )
        self.assertEqual(row_value(row, "audit_date"), ON_DATE)
        self.assertEqual(row_value(row, "paid_date"), "2025-11-06")
        # posting_date keeps the original postings, oldest first
        self.assertTrue(row_value(row, "posting_date").startswith("2025-11-06 00:30"))

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
    """Rows come from folios carrying a room charge, and only from those.

    The report used to select reservations -- in house, plus any with activity
    that day -- and then look up each one's folio. It now selects folios
    directly, so a reservation with no folio can no longer produce a row. That
    is the point: a stay with no room charge is not a room charge.
    """

    def folio_row(self, folio, name=None):
        return frappe._dict(
            folio=folio, name=name or folio, customer_id="CUST-1",
            room_type="", bed_type="", actual_room_id="", channel="Walk In",
            bill_instructions=None,
        )

    def test_no_selected_folios_produces_no_rows(self):
        with mock.patch.object(audit_report, "get_folio_rows", return_value=[]):
            self.assertEqual(audit_report.get_data_detail(ON_DATE, 1), [])

    def test_one_row_is_emitted_per_selected_folio(self):
        rows = [self.folio_row("F-1", "RSV-1"), self.folio_row("F-2", "RSV-2")]

        with mock.patch.object(audit_report, "get_folio_rows", return_value=rows), \
                mock.patch.object(audit_report, "get_transactions_for_date", return_value=[]), \
                mock.patch.object(audit_report, "get_settlement_status", return_value={}):
            out = audit_report.get_data_detail(ON_DATE, 1)

        self.assertEqual(len(out), 2)
        self.assertEqual([row_value(row, "rsv") for row in out], ["RSV-1", "RSV-2"])
        for row in out:
            self.assertEqual(len(row), len(audit_report.get_columns()))
            self.assertEqual(row_value(row, "audit_date"), ON_DATE)

    def test_every_selected_folio_is_queried_for_its_transactions(self):
        rows = [self.folio_row("F-1"), self.folio_row("F-2")]

        with mock.patch.object(audit_report, "get_folio_rows", return_value=rows), \
                mock.patch.object(audit_report, "get_transactions_for_date",
                                  return_value=[]) as transactions, \
                mock.patch.object(audit_report, "get_settlement_status", return_value={}):
            audit_report.get_data_detail(ON_DATE, 1)

        self.assertEqual(sorted(transactions.call_args[0][0]), ["F-1", "F-2"])
        self.assertEqual(transactions.call_args[0][1], ON_DATE)


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
