# -*- coding: utf-8 -*-
# Copyright (c) 2025, PMM and Contributors
# See license.txt
"""Tests for the Front Desk Daily Revenue Recap.

Covers the arithmetic that replaces the spreadsheet's hand-built totals. The
money and occupancy queries need a database, so these tests exercise the pure
row/column shaping and are safe to run anywhere.
"""

from __future__ import unicode_literals

import unittest

import frappe

from inn.helper.revenue_lines import (
    LINE_BANQUET,
    LINE_LAUNDRY,
    LINE_OTHER,
    LINE_RESTAURANT,
    LINE_ROOM_SERVICE,
)
from inn.inn_hotels.report.front_desk_daily_revenue_recap import (
    front_desk_daily_revenue_recap as recap,
)


class TestHelpers(unittest.TestCase):
    def test_mode_fieldname_is_stable_and_safe(self):
        self.assertEqual(recap.mode_fieldname("BCA EDC"), "mode_bca_edc")
        self.assertEqual(recap.mode_fieldname("City Ledger"), "mode_city_ledger")
        self.assertEqual(recap.mode_fieldname("Cash"), "mode_cash")

    def test_daterange_is_inclusive(self):
        dates = list(recap.daterange(frappe.utils.getdate("2025-11-01"),
                                     frappe.utils.getdate("2025-11-03")))
        self.assertEqual([str(d) for d in dates],
                         ["2025-11-01", "2025-11-02", "2025-11-03"])

    def test_daterange_single_day(self):
        dates = list(recap.daterange(frappe.utils.getdate("2025-11-01"),
                                     frappe.utils.getdate("2025-11-01")))
        self.assertEqual(len(dates), 1)

    def test_inverted_range_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            recap.get_date_range(frappe._dict(from_date="2025-11-30", to_date="2025-11-01"))

    def test_collection_delta_has_every_ar_key(self):
        delta = recap.collection_delta()
        for key in ("ar_created", "ar_settled", "ar_closing", "guest_credit_closing"):
            self.assertIn(key, delta)


class TestMoneyColumns(unittest.TestCase):
    def setUp(self):
        self.money = recap.new_money_bucket()
        self.money["room_revenue_nett"] = 1000
        self.money["room_revenue_tax_service"] = 210
        self.money["room_revenue_gross"] = 1210
        self.money["breakfast_nett"] = 80
        self.money["breakfast_tax_service"] = 17
        self.money["breakfast_gross"] = 97
        self.money[LINE_RESTAURANT] = 500
        self.money[LINE_ROOM_SERVICE] = 50
        self.money[LINE_BANQUET] = 100
        self.money[LINE_LAUNDRY] = 20
        self.money[LINE_OTHER] = 13
        self.money["commission"] = 121
        self.money["total_collected"] = 1500
        self.money["modes"] = {"Cash": 900, "BCA EDC": 600}

    def test_gross_equals_nett_plus_tax_service(self):
        row = recap.build_money_columns(self.money, ["Cash", "BCA EDC"])
        self.assertEqual(row["room_revenue_gross"], 1210)
        self.assertEqual(row["breakfast_gross"], 97)

    def test_total_revenue_sums_the_revenue_lines_only(self):
        row = recap.build_money_columns(self.money, ["Cash", "BCA EDC"])
        self.assertEqual(row["total_revenue_gross"], 1210 + 97 + 500 + 50 + 100 + 20 + 13)

    def test_commission_is_a_deduction_not_a_revenue_line(self):
        row = recap.build_money_columns(self.money, ["Cash", "BCA EDC"])
        self.assertEqual(row["net_revenue_after_commission"],
                         row["total_revenue_gross"] - 121)

    def test_modes_become_their_own_columns(self):
        row = recap.build_money_columns(self.money, ["Cash", "BCA EDC", "Voucher"])
        self.assertEqual(row["mode_cash"], 900)
        self.assertEqual(row["mode_bca_edc"], 600)
        self.assertEqual(row["mode_voucher"], 0)
        self.assertEqual(row["cash_collected"], 900)

    def test_missing_line_defaults_to_zero(self):
        row = recap.build_money_columns(recap.new_money_bucket(), [])
        self.assertEqual(row["total_revenue_gross"], 0)
        self.assertEqual(row["total_collected"], 0)


class TestColumnSets(unittest.TestCase):
    def test_date_view_has_occupancy_ratios_and_receivables(self):
        names = [c["fieldname"] for c in recap.get_columns(recap.GROUP_BY_DATE, ["Cash"])]
        for expected in ("rooms_available", "room_nights_sold", "occupancy_pct",
                         "adr", "revpar", "ar_closing", "guest_credit_closing",
                         "difference", "mode_cash"):
            self.assertIn(expected, names)
        self.assertNotIn("channel", names)

    def test_channel_view_marks_the_channel_and_drops_hotel_level_columns(self):
        names = [c["fieldname"] for c in
                 recap.get_columns(recap.GROUP_BY_DATE_CHANNEL, ["Cash"])]
        self.assertIn("channel", names)
        # Occupancy and arrivals are not attributable to a channel.
        for absent in ("room_nights_sold", "occupancy_pct", "adr", "revpar",
                       "arrivals", "in_house"):
            self.assertNotIn(absent, names)
        # Receivables are folio-level, also not attributable.
        self.assertNotIn("ar_closing", names)
        # Money columns stay.
        self.assertIn("total_collected", names)
        self.assertIn("difference", names)

    def test_every_column_has_a_label_and_fieldname(self):
        for group_by in recap.GROUP_BY_OPTIONS:
            for column in recap.get_columns(group_by, ["Cash", "BCA EDC"]):
                self.assertTrue(column.get("fieldname"), column)
                self.assertTrue(column.get("label"), column)


class TestReportSummary(unittest.TestCase):
    def setUp(self):
        self.totals = recap.new_totals()
        self.totals["room_revenue_gross"] = 1000000
        self.totals["total_revenue_gross"] = 1200000
        self.totals["total_collected"] = 900000
        self.totals["room_nights_sold"] = 50
        self.totals["rooms_available"] = 100
        self.totals["ar_closing"] = 250000

    def summary(self):
        return {s["label"]: s["value"] for s in
                recap.build_report_summary(self.totals, {})}

    def test_adr_is_a_true_mean(self):
        """Regression: Daily Flash Report averages with (avg + rate) / 2."""
        self.assertEqual(self.summary()["ADR"], 1000000 / 50)

    def test_revpar_uses_saleable_rooms(self):
        self.assertEqual(self.summary()["RevPAR"], 1000000 / 100)

    def test_occupancy_is_a_percentage(self):
        self.assertEqual(self.summary()["Occupancy %"], 50.0)

    def test_difference_is_recognised_minus_collected(self):
        self.assertEqual(self.summary()["Recognised - Collected"], 300000)

    def test_zero_room_nights_does_not_divide_by_zero(self):
        self.totals["room_nights_sold"] = 0
        self.assertEqual(self.summary()["ADR"], 0)

    def test_zero_saleable_rooms_does_not_divide_by_zero(self):
        self.totals["rooms_available"] = 0
        summary = self.summary()
        self.assertEqual(summary["RevPAR"], 0)
        self.assertEqual(summary["Occupancy %"], 0)


class TestRowActivity(unittest.TestCase):
    def test_money_alone_counts_as_activity(self):
        money = recap.new_money_bucket()
        money["total_collected"] = 1
        self.assertTrue(recap.row_has_activity(None, None, money))

    def test_quiet_day_is_not_activity(self):
        self.assertFalse(recap.row_has_activity(
            recap.empty_occupancy(), recap.empty_arrivals(), recap.new_money_bucket()))

    def test_occupancy_alone_counts_as_activity(self):
        occupancy = recap.empty_occupancy()
        occupancy["room_nights_sold"] = 3
        self.assertTrue(recap.row_has_activity(
            occupancy, recap.empty_arrivals(), recap.new_money_bucket()))


class TestFilterContract(unittest.TestCase):
    """The report's JS and Python must agree on the interface.

    The Desk evals the JS and then looks the report up by name, so a wrong key
    or a filter missing from the JS fails *silently* - an empty filter bar or an
    unreachable option rather than an error. These assertions make that drift
    loud. Verified by hand first: evaluating the JS registers exactly one entry,
    "Front Desk Daily Revenue Recap", with the four expected filters.
    """

    FILTER_FIELDS = ("from_date", "to_date", "group_by", "include_zero_rows")

    def js_source(self):
        import os

        path = os.path.join(
            os.path.dirname(recap.__file__), "front_desk_daily_revenue_recap.js"
        )
        with open(path) as handle:
            return handle.read()

    def test_js_registers_under_the_exact_report_name(self):
        self.assertIn(
            'frappe.query_reports["{0}"]'.format(recap.REPORT_NAME), self.js_source()
        )

    def test_js_declares_every_filter_the_python_reads(self):
        source = self.js_source()
        for fieldname in self.FILTER_FIELDS:
            self.assertIn('fieldname: "{0}"'.format(fieldname), source, fieldname)

    def test_js_offers_every_group_by_option_python_accepts(self):
        expected = "options: [{0}]".format(
            ", ".join('"{0}"'.format(option) for option in recap.GROUP_BY_OPTIONS)
        )
        self.assertIn(expected, self.js_source())


class TestOccupancyDeduplication(unittest.TestCase):
    def test_room_move_overlap_same_reservation_counted_once(self):
        """Room move overlap (old Finished + new Stayed) must deduplicate to 1 room night."""
        dates = [frappe.utils.getdate("2026-10-31")]
        inventory = 120
        bookings = [
            {
                "start": "2026-10-28",
                "end": "2026-11-02",
                "room_id": "R-1012",
                "reference_name": "RSV-29690",
                "room_availability": recap.ROOM_SOLD,
                "status": "Finished",
            },
            {
                "start": "2026-10-31",
                "end": "2026-11-02",
                "room_id": "R-1014",
                "reference_name": "RSV-29690",
                "room_availability": recap.ROOM_SOLD,
                "status": "Stayed",
            },
        ]

        occupancy = recap.calculate_room_occupancy_counts(dates, inventory, bookings)
        day_bucket = occupancy[dates[0]]

        self.assertEqual(day_bucket["room_nights_sold"], 1)

    def test_same_physical_room_overlap_counted_once(self):
        """Two bookings claiming the same physical room on the same date count as 1 room night."""
        dates = [frappe.utils.getdate("2026-10-31")]
        inventory = 120
        bookings = [
            {
                "start": "2026-10-30",
                "end": "2026-11-01",
                "room_id": "R-1012",
                "reference_name": "RSV-001",
                "room_availability": recap.ROOM_SOLD,
                "status": "Finished",
            },
            {
                "start": "2026-10-31",
                "end": "2026-11-02",
                "room_id": "R-1012",
                "reference_name": "RSV-002",
                "room_availability": recap.ROOM_SOLD,
                "status": "Stayed",
            },
        ]

        occupancy = recap.calculate_room_occupancy_counts(dates, inventory, bookings)
        day_bucket = occupancy[dates[0]]

        self.assertEqual(day_bucket["room_nights_sold"], 1)

    def test_occupancy_never_exceeds_inventory_ceiling(self):
        """Room nights sold can never exceed total rooms inventory."""
        dates = [frappe.utils.getdate("2026-10-31")]
        inventory = 5
        # 10 bookings for 5 rooms
        bookings = [
            {
                "start": "2026-10-31",
                "end": "2026-11-01",
                "room_id": "R-{0}".format(i),
                "reference_name": "RSV-{0}".format(i),
                "room_availability": recap.ROOM_SOLD,
                "status": "Stayed",
            }
            for i in range(10)
        ]

        occupancy = recap.calculate_room_occupancy_counts(dates, inventory, bookings)
        day_bucket = occupancy[dates[0]]

        self.assertLessEqual(day_bucket["room_nights_sold"], 5)

    def test_zero_inventory_has_zero_sold_room_nights(self):
        """No physical inventory must not allow sold-room counts to grow."""
        dates = [frappe.utils.getdate("2026-10-31")]
        bookings = [
            {
                "start": "2026-10-31",
                "end": "2026-11-01",
                "room_id": "R-101",
                "reference_name": "RSV-001",
                "room_availability": recap.ROOM_SOLD,
                "status": "Stayed",
            }
        ]

        occupancy = recap.calculate_room_occupancy_counts(dates, 0, bookings)
        self.assertEqual(occupancy[dates[0]]["room_nights_sold"], 0)

    def test_out_of_order_deduplication(self):
        """Duplicate Out of Order bookings on the same room are counted once."""
        dates = [frappe.utils.getdate("2026-10-31")]
        inventory = 10
        bookings = [
            {
                "start": "2026-10-30",
                "end": "2026-11-02",
                "room_id": "R-101",
                "reference_name": None,
                "room_availability": recap.ROOM_OUT_OF_ORDER,
                "status": "Booked",
            },
            {
                "start": "2026-10-31",
                "end": "2026-11-03",
                "room_id": "R-101",
                "reference_name": None,
                "room_availability": recap.ROOM_OUT_OF_ORDER,
                "status": "Booked",
            },
        ]

        occupancy = recap.calculate_room_occupancy_counts(dates, inventory, bookings)
        day_bucket = occupancy[dates[0]]

        self.assertEqual(day_bucket["rooms_out_of_order"], 1)

