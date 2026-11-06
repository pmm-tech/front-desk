# Copyright (c) 2025, PMM and contributors
# For license information, please see license.txt
"""Front Desk Daily Revenue Recap.

One row per business date, so the front desk can close a day and finance can sum
the month without rebuilding it by hand.

Why this exists
---------------
``REPORT UANG MASUK REKENING`` is assembled manually from daily ``Audit Report``
exports. Two of its structural problems are fixed here by construction:

* the same month carries two different "room revenue" totals, and the rule that
  reconciles them (revenue recognised vs money collected) lives only in font
  colour. Here both figures are columns and their difference is its own column;
* occupancy, ADR and RevPAR are absent from the spreadsheet entirely, and the
  ADR that ``Daily Flash Report`` prints is a running pairwise mean rather than
  a real average. Both are computed properly here.

The business date is ``Inn Folio Transaction.audit_date``, set at day-end close
from ``Inn Audit Log``. Payment ``creation`` timestamps run past midnight, so
``creation`` is the wall-clock date and ``audit_date`` is the business date.
Reporting on the wrong one shifts revenue between days.

Column groups
-------------
``Date``             full column set, one row per date.
``Date + Channel``   date x ``Inn Channel``; hotel-level occupancy and arrival
                     counts are not attributable to a channel, so those columns
                     are omitted rather than shown empty.
"""

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, flt, getdate, today

from inn.helper.revenue_lines import (
    LINE_BANQUET,
    LINE_BREAKFAST,
    LINE_COMMISSION,
    LINE_LAUNDRY,
    LINE_OTHER,
    LINE_REFUND,
    LINE_RESTAURANT,
    LINE_ROOM,
    LINE_ROOM_SERVICE,
    classify,
    get_line_map,
    get_settings_context,
    get_tax_service_types,
    unmapped_types,
)

REPORT_NAME = "Front Desk Daily Revenue Recap"

GROUP_BY_DATE = "Date"
GROUP_BY_DATE_CHANNEL = "Date + Channel"
GROUP_BY_OPTIONS = (GROUP_BY_DATE, GROUP_BY_DATE_CHANNEL)

# Inn Room Booking.room_availability
ROOM_SOLD = "Room Sold"
ROOM_COMPLIMENTARY = "Room Compliment"
ROOM_HOUSE_USE = "House Use"
ROOM_OUT_OF_ORDER = "Out of Order"

# Inn Room Booking.status values that count as an occupied room
SOLD_BOOKING_STATUSES = ("Stayed", "Finished")

UNASSIGNED_CHANNEL = "Unassigned"

#: Preferred left-to-right order for the Mode of Payment columns. This only
#: affects ordering; which modes appear is read from the data, and a mode not
#: listed here is appended alphabetically.
MODE_ORDER = (
    "Cash",
    "BCA EDC",
    "BCA QRIS",
    "BCA TRANSFER",
    "MANDIRI EDC",
    "MANDIRI QRIS",
    "MANDIRI TRANSFER",
    "City Ledger",
    "Voucher",
    "Cheque",
    "Credit Card",
    "Wire Transfer",
    "Bank Draft",
)

#: Revenue lines that are split into nett and tax+service columns.
SPLIT_LINES = {
    LINE_ROOM: ("room_revenue_nett", "room_revenue_tax_service", "room_revenue_gross"),
    LINE_BREAKFAST: ("breakfast_nett", "breakfast_tax_service", "breakfast_gross"),
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def execute(filters=None):
    filters = filters or frappe._dict()

    from_date, to_date = get_date_range(filters)
    group_by = filters.get("group_by") or GROUP_BY_DATE
    if group_by not in GROUP_BY_OPTIONS:
        group_by = GROUP_BY_DATE

    include_zero_rows = filters.get("include_zero_rows")
    include_zero_rows = 1 if include_zero_rows is None else int(include_zero_rows)

    modes = get_used_modes(from_date, to_date)
    context = get_settings_context()
    line_map = get_line_map(context)
    tax_service_types = get_tax_service_types(context)

    dates = list(daterange(from_date, to_date))

    occupancy, arrivals = get_occupancy_and_arrivals(dates)
    buckets, unmapped = get_money_buckets(
        from_date, to_date, line_map, tax_service_types, group_by
    )
    ar = get_ar_movement(dates, from_date, to_date)
    ar_opening = ar["opening"]
    guest_credit_opening = ar["deposits_opening"]
    ar_by_date = ar["by_date"]

    declare_unmapped(unmapped)

    data = []
    totals = new_totals()

    for business_date in dates:
        occupancy_row = occupancy.get(business_date, empty_occupancy())
        arrivals_row = arrivals.get(business_date, empty_arrivals())

        if group_by == GROUP_BY_DATE_CHANNEL:
            channels = buckets.get(business_date) or {UNASSIGNED_CHANNEL: {}}
            for channel in sorted(channels):
                row = build_channel_row(
                    business_date,
                    channel,
                    channels[channel],
                    modes,
                    include_zero_rows,
                )
                if row is None:
                    continue
                accumulate_channel_totals(totals, row, modes)
                data.append(row)
        else:
            # Date view merges every channel into UNASSIGNED_CHANNEL.
            date_buckets = buckets.get(business_date) or {}
            row = build_date_row(
                business_date,
                occupancy_row,
                arrivals_row,
                date_buckets.get(UNASSIGNED_CHANNEL) or {},
                modes,
                ar_by_date.get(business_date, collection_delta()),
                ar_opening,
                guest_credit_opening,
                include_zero_rows,
            )
            if row is None:
                continue
            accumulate_date_totals(totals, row, modes)
            data.append(row)

    report_summary = (
        build_report_summary(totals, occupancy) if group_by == GROUP_BY_DATE else None
    )

    return get_columns(group_by, modes), data, None, None, report_summary


# ---------------------------------------------------------------------------
# Filters / helpers
# ---------------------------------------------------------------------------
def get_date_range(filters):
    from_date = filters.get("from_date") or get_last_audit_date() or today()
    to_date = filters.get("to_date") or from_date

    from_date = getdate(from_date)
    to_date = getdate(to_date)

    if from_date > to_date:
        frappe.throw(_("From Date cannot be after To Date"))

    return from_date, to_date


def get_last_audit_date():
    from inn.inn_hotels.doctype.inn_audit_log.inn_audit_log import get_last_audit_date

    return get_last_audit_date()


def daterange(from_date, to_date):
    """Yield every date from ``from_date`` to ``to_date`` inclusive."""
    for offset in range(date_diff(to_date, from_date) + 1):
        yield add_days(from_date, offset)


def mode_fieldname(mode):
    return "mode_{0}".format(frappe.scrub(mode or UNASSIGNED_CHANNEL))


def get_used_modes(from_date, to_date):
    rows = frappe.db.sql(
        """
        select distinct mode_of_payment
        from `tabInn Folio Transaction`
        where audit_date between %s and %s
          and is_void = 0
          and flag = 'Credit'
          and mode_of_payment is not null
          and mode_of_payment != ''
        """,
        (from_date, to_date),
    )
    modes = [row[0] for row in rows]

    def sort_key(mode):
        try:
            return (0, MODE_ORDER.index(mode), "")
        except ValueError:
            return (1, 0, mode)

    return sorted(modes, key=sort_key)


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------
def get_columns(group_by, modes):
    columns = []

    if group_by == GROUP_BY_DATE_CHANNEL:
        columns.append(
            {"fieldname": "channel", "label": _("Channel"), "fieldtype": "Data", "width": 160}
        )

    columns.append(
        {"fieldname": "business_date", "label": _("Business Date"), "fieldtype": "Date", "width": 110}
    )

    if group_by == GROUP_BY_DATE:
        columns.extend(
            [
                {"fieldname": "rooms_inventory", "label": _("Rooms"), "fieldtype": "Int", "width": 80},
                {"fieldname": "rooms_out_of_order", "label": _("Out of Order"), "fieldtype": "Int", "width": 100},
                {"fieldname": "rooms_available", "label": _("Saleable"), "fieldtype": "Int", "width": 90},
                {"fieldname": "room_nights_sold", "label": _("Room Nights"), "fieldtype": "Int", "width": 100},
                {"fieldname": "room_nights_complimentary", "label": _("Complimentary"), "fieldtype": "Int", "width": 110},
                {"fieldname": "room_nights_house_use", "label": _("House Use"), "fieldtype": "Int", "width": 100},
                {"fieldname": "day_use", "label": _("Day Use"), "fieldtype": "Int", "width": 90},
                {"fieldname": "occupancy_pct", "label": _("Occupancy %"), "fieldtype": "Percent", "width": 110},
                {"fieldname": "arrivals", "label": _("Arrivals"), "fieldtype": "Int", "width": 90},
                {"fieldname": "departures", "label": _("Departures"), "fieldtype": "Int", "width": 100},
                {"fieldname": "in_house", "label": _("In House"), "fieldtype": "Int", "width": 90},
                {"fieldname": "no_shows", "label": _("No Shows"), "fieldtype": "Int", "width": 90},
            ]
        )

    columns.extend(
        [
            {"fieldname": "room_revenue_nett", "label": _("Room Revenue Nett"), "fieldtype": "Currency", "width": 140},
            {"fieldname": "room_revenue_tax_service", "label": _("Room Tax+Service"), "fieldtype": "Currency", "width": 140},
            {"fieldname": "room_revenue_gross", "label": _("Room Revenue Gross"), "fieldtype": "Currency", "width": 150},
            {"fieldname": "breakfast_nett", "label": _("Breakfast Nett"), "fieldtype": "Currency", "width": 130},
            {"fieldname": "breakfast_tax_service", "label": _("Breakfast Tax+Service"), "fieldtype": "Currency", "width": 150},
            {"fieldname": "breakfast_gross", "label": _("Breakfast Gross"), "fieldtype": "Currency", "width": 140},
            {"fieldname": "restaurant_revenue", "label": _("Restaurant"), "fieldtype": "Currency", "width": 130},
            {"fieldname": "room_service_revenue", "label": _("Room Service"), "fieldtype": "Currency", "width": 130},
            {"fieldname": "banquet_revenue", "label": _("Banquet"), "fieldtype": "Currency", "width": 130},
            {"fieldname": "laundry_revenue", "label": _("Laundry"), "fieldtype": "Currency", "width": 120},
            {"fieldname": "other_revenue", "label": _("Other"), "fieldtype": "Currency", "width": 120},
            {"fieldname": "total_revenue_gross", "label": _("Total Revenue"), "fieldtype": "Currency", "width": 150},
            {"fieldname": "commission", "label": _("Commission"), "fieldtype": "Currency", "width": 130},
            {"fieldname": "net_revenue_after_commission", "label": _("Net After Commission"), "fieldtype": "Currency", "width": 160},
        ]
    )

    if group_by == GROUP_BY_DATE:
        columns.extend(
            [
                {"fieldname": "adr", "label": _("ADR"), "fieldtype": "Currency", "width": 120},
                {"fieldname": "revpar", "label": _("RevPAR"), "fieldtype": "Currency", "width": 120},
            ]
        )

    for mode in modes:
        columns.append(
            {
                "fieldname": mode_fieldname(mode),
                "label": mode,
                "fieldtype": "Currency",
                "width": 130,
            }
        )

    columns.extend(
        [
            {"fieldname": "total_collected", "label": _("Total Collected"), "fieldtype": "Currency", "width": 150},
            {"fieldname": "cash_collected", "label": _("Cash Collected"), "fieldtype": "Currency", "width": 140},
            {"fieldname": "revenue_recognised", "label": _("Revenue Recognised"), "fieldtype": "Currency", "width": 155},
            {"fieldname": "difference", "label": _("Recognised - Collected"), "fieldtype": "Currency", "width": 175},
        ]
    )

    if group_by == GROUP_BY_DATE:
        # Unsettled movement is a folio-level figure and cannot be attributed to
        # an Inn Channel, so these columns exist only in the Date view.
        columns.extend(
            [
                {"fieldname": "ar_opening", "label": _("Receivable Opening"), "fieldtype": "Currency", "width": 155},
                {"fieldname": "ar_created", "label": _("Charged"), "fieldtype": "Currency", "width": 140},
                {"fieldname": "ar_settled", "label": _("Settled"), "fieldtype": "Currency", "width": 130},
                {"fieldname": "ar_closing", "label": _("Receivable Closing"), "fieldtype": "Currency", "width": 155},
                {"fieldname": "guest_credit_closing", "label": _("Guest Credit Held"), "fieldtype": "Currency", "width": 150},
            ]
        )

    return columns


def calculate_room_occupancy_counts(dates, inventory, bookings):
    """Calculate daily room occupancy counts with defensive deduplication.

    Protects against duplicate counting when multiple bookings exist for
    the same physical room or reservation on the same business date (e.g. from
    overlapping room move segments).
    Ensures room_nights_sold never exceeds physical inventory.
    """
    occupancy = {
        business_date: {
            "rooms_inventory": inventory,
            "rooms_out_of_order": 0,
            "rooms_available": inventory,
            "room_nights_sold": 0,
            "room_nights_complimentary": 0,
            "room_nights_house_use": 0,
            "day_use": 0,
            "occupancy_pct": 0.0,
        }
        for business_date in dates
    }

    if not dates:
        return occupancy

    first, last = dates[0], dates[-1]

    # Track seen (date, room_id) and (date, reference_name) to avoid double counting
    seen_sold_rooms = {d: set() for d in dates}
    seen_sold_reservations = {d: set() for d in dates}
    seen_ooo_rooms = {d: set() for d in dates}

    # Sort bookings to prioritize 'Stayed' over 'Finished' over others
    def booking_sort_key(b):
        st = b.get("status")
        if st == "Stayed":
            return 0
        elif st == "Finished":
            return 1
        elif st == "Booked":
            return 2
        return 3

    sorted_bookings = sorted(bookings, key=booking_sort_key)

    for booking in sorted_bookings:
        start_date = getdate(booking.get("start")) if booking.get("start") else None
        end_date = getdate(booking.get("end")) if booking.get("end") else None
        if not start_date or not end_date or start_date >= end_date:
            continue

        start = max(start_date, first)
        end = min(end_date, add_days(last, 1))

        availability = booking.get("room_availability")
        status = booking.get("status")
        room_id = booking.get("room_id")
        ref_name = booking.get("reference_name")

        for business_date in daterange(start, add_days(end, -1)):
            bucket = occupancy.get(business_date)
            if bucket is None:
                continue

            if availability == ROOM_OUT_OF_ORDER:
                if room_id:
                    if room_id in seen_ooo_rooms[business_date]:
                        continue
                    seen_ooo_rooms[business_date].add(room_id)
                bucket["rooms_out_of_order"] += 1
            elif availability == ROOM_HOUSE_USE:
                bucket["room_nights_house_use"] += 1
            elif availability == ROOM_COMPLIMENTARY:
                bucket["room_nights_complimentary"] += 1
            elif availability == ROOM_SOLD and status in SOLD_BOOKING_STATUSES:
                # Deduplicate by physical room and reservation
                if room_id and room_id in seen_sold_rooms[business_date]:
                    continue
                if ref_name and ref_name in seen_sold_reservations[business_date]:
                    continue

                if room_id:
                    seen_sold_rooms[business_date].add(room_id)
                if ref_name:
                    seen_sold_reservations[business_date].add(ref_name)

                # Room nights sold can never exceed physical inventory.
                if bucket["room_nights_sold"] < bucket["rooms_inventory"]:
                    bucket["room_nights_sold"] += 1

    for business_date, bucket in occupancy.items():
        bucket["rooms_available"] = max(
            bucket["rooms_inventory"] - bucket["rooms_out_of_order"], 0
        )
        if bucket["rooms_available"]:
            bucket["occupancy_pct"] = round(
                100.0 * bucket["room_nights_sold"] / bucket["rooms_available"], 2
            )

    return occupancy


# ---------------------------------------------------------------------------
# Occupancy and arrivals
# ---------------------------------------------------------------------------
def get_occupancy_and_arrivals(dates):
    # Keep room counts as ints: the columns are Int-typed.
    inventory = int(frappe.db.sql("select count(*) from `tabInn Room`")[0][0] or 0)

    first, last = dates[0], dates[-1]

    bookings = frappe.db.sql(
        """
        select start, end, room_id, reference_name, room_availability, status
        from `tabInn Room Booking`
        where start <= %s and end > %s
        """,
        (last, first),
        as_dict=True,
    )

    occupancy = calculate_room_occupancy_counts(dates, inventory, bookings)

    arrivals = {business_date: empty_arrivals() for business_date in dates}

    for reservation in get_reservations_for_range(first, last):
        arrival_date = getdate(reservation.arrival) if reservation.arrival else None
        departure_date = getdate(reservation.departure) if reservation.departure else None
        expected_arrival = (
            getdate(reservation.expected_arrival) if reservation.expected_arrival else None
        )

        for business_date in dates:
            bucket = arrivals.get(business_date)
            if bucket is None:
                continue

            if arrival_date == business_date:
                bucket["arrivals"] += 1
            if departure_date == business_date:
                bucket["departures"] += 1
            if arrival_date and arrival_date == departure_date == business_date:
                bucket["day_use"] += 1
            if (
                reservation.status == "No Show"
                and expected_arrival
                and expected_arrival == business_date
            ):
                bucket["no_shows"] += 1
            if reservation.status in ("In House", "Finish") and arrival_date:
                if arrival_date <= business_date and (
                    departure_date is None or departure_date > business_date
                ):
                    bucket["in_house"] += 1

    for business_date, bucket in occupancy.items():
        bucket["rooms_available"] = max(
            bucket["rooms_inventory"] - bucket["rooms_out_of_order"], 0
        )
        if bucket["rooms_available"]:
            bucket["occupancy_pct"] = round(
                100.0 * bucket["room_nights_sold"] / bucket["rooms_available"], 2
            )
        bucket["day_use"] = arrivals[business_date]["day_use"]

    return occupancy, arrivals


def get_reservations_for_range(from_date, to_date):
    return frappe.db.sql(
        """
        select name, status, arrival, departure, expected_arrival, expected_departure
        from `tabInn Reservation`
        where (date(arrival) between %s and %s)
           or (date(departure) between %s and %s)
           or status in ('In House', 'Finish')
           or (status = 'No Show' and expected_arrival between %s and %s)
        """,
        (from_date, to_date, from_date, to_date, from_date, to_date),
        as_dict=True,
    )


def empty_occupancy():
    return {
        "rooms_inventory": 0,
        "rooms_out_of_order": 0,
        "rooms_available": 0,
        "room_nights_sold": 0,
        "room_nights_complimentary": 0,
        "room_nights_house_use": 0,
        "day_use": 0,
        "occupancy_pct": 0.0,
    }


def empty_arrivals():
    return {"arrivals": 0, "departures": 0, "in_house": 0, "no_shows": 0, "day_use": 0}


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def get_money_buckets(from_date, to_date, line_map, tax_service_types, group_by):
    """Aggregate transactions into ``{date: {channel: {bucket: amount}}}``.

    One grouped query for the whole window; classification happens in Python so
    an unmapped transaction type can be surfaced rather than silently dropped.
    """
    rows = frappe.db.sql(
        """
        select trx.audit_date, trx.transaction_type, trx.flag, trx.mode_of_payment,
               coalesce(res.channel, folio.channel, %s) as channel,
               sum(trx.amount) as amount
        from `tabInn Folio Transaction` trx
        inner join `tabInn Folio` folio on folio.name = trx.parent
        left join `tabInn Reservation` res on res.name = folio.reservation_id
        where trx.audit_date between %s and %s
          and trx.is_void = 0
        group by trx.audit_date, trx.transaction_type, trx.flag, trx.mode_of_payment, channel
        """,
        (UNASSIGNED_CHANNEL, from_date, to_date),
        as_dict=True,
    )

    buckets = {}
    # Payment types have no revenue line by design, so only charge types can be
    # "unmapped".
    charge_types = {
        row.transaction_type
        for row in rows
        if (row.flag or "").strip().lower() != "credit"
    }

    for row in rows:
        business_date = getdate(row.audit_date)
        channel = row.channel or UNASSIGNED_CHANNEL
        if group_by == GROUP_BY_DATE:
            channel = UNASSIGNED_CHANNEL

        bucket = buckets.setdefault(business_date, {}).setdefault(
            channel, new_money_bucket()
        )

        amount = flt(row.amount)

        if (row.flag or "").strip().lower() == "credit":
            bucket["total_collected"] += amount
            if row.mode_of_payment:
                bucket["modes"][row.mode_of_payment] = (
                    bucket["modes"].get(row.mode_of_payment, 0) + amount
                )
            continue

        line = classify(row.transaction_type, line_map)

        if line == LINE_COMMISSION:
            bucket["commission"] += amount
        elif line == LINE_REFUND:
            # Refunds are debits in this app; kept out of revenue but visible in
            # the unsettled movement.
            bucket["refund"] += amount
        elif line in SPLIT_LINES:
            nett_field, tax_field, gross_field = SPLIT_LINES[line]
            if row.transaction_type in tax_service_types:
                bucket[tax_field] += amount
            else:
                bucket[nett_field] += amount
            bucket[gross_field] += amount
        else:
            bucket[line] = bucket.get(line, 0) + amount

    unmapped = unmapped_types(charge_types, line_map)
    return buckets, unmapped


def new_money_bucket():
    return {
        LINE_ROOM: 0,
        LINE_BREAKFAST: 0,
        LINE_RESTAURANT: 0,
        LINE_ROOM_SERVICE: 0,
        LINE_BANQUET: 0,
        LINE_LAUNDRY: 0,
        LINE_OTHER: 0,
        "room_revenue_nett": 0,
        "room_revenue_tax_service": 0,
        "room_revenue_gross": 0,
        "breakfast_nett": 0,
        "breakfast_tax_service": 0,
        "breakfast_gross": 0,
        "commission": 0,
        "refund": 0,
        "total_collected": 0,
        "modes": {},
    }


def declare_unmapped(unmapped):
    if unmapped:
        frappe.msgprint(
            msg=_("Transaction types with no revenue mapping, counted as Other: {0}").format(
                ", ".join(unmapped)
            ),
            title=_("Unmapped transaction types"),
            indicator="orange",
        )


def get_ar_movement(dates, from_date, to_date):
    """Folio receivables and guest credit, reconstructed as at each date.

    Two figures are tracked separately, because netting them hides both:

    ``ar_*``          money owed *to* the hotel: the sum of positive per-folio
                      balances (debits in excess of credits). This is the
                      "unpaid booking" figure the spreadsheet reports.
    ``guest_credit_*`` money held *for* guests: advance deposits and other
                      credits in excess of charges, which are a liability and
                      grow whenever future stays are prepaid.

    ``AR City Ledger.is_paid`` cannot be used - every row is 0 - and
    ``AR City Ledger Invoice`` has no rows, so the balances are rebuilt from the
    transactions themselves. ``Inn Folio.balance`` is current state only and
    would be wrong when re-running a past period.
    """
    balances = {}

    for row in frappe.db.sql(
        """
        select parent,
               sum(case when flag = 'Debit' then amount else 0 end) as debit,
               sum(case when flag = 'Credit' then amount else 0 end) as credit
        from `tabInn Folio Transaction`
        where is_void = 0 and audit_date < %s
        group by parent
        """,
        (from_date,),
        as_dict=True,
    ):
        balances[row.parent] = flt(row.debit) - flt(row.credit)

    receivables = sum(balance for balance in balances.values() if balance > 0)
    deposits = sum(-balance for balance in balances.values() if balance < 0)

    deltas = {}
    for row in frappe.db.sql(
        """
        select parent, audit_date,
               sum(case when flag = 'Debit' then amount else 0 end) as debit,
               sum(case when flag = 'Credit' then amount else 0 end) as credit
        from `tabInn Folio Transaction`
        where is_void = 0 and audit_date between %s and %s
        group by parent, audit_date
        order by audit_date
        """,
        (from_date, to_date),
        as_dict=True,
    ):
        deltas.setdefault(getdate(row.audit_date), []).append(row)

    by_date = {}
    for business_date in dates:
        created = 0.0
        settled = 0.0

        for row in deltas.get(business_date, []):
            debit = flt(row.debit)
            credit = flt(row.credit)
            created += debit
            settled += credit

            previous = balances.get(row.parent, 0.0)
            current = previous + debit - credit
            balances[row.parent] = current

            # Adjust the running sums by the change only, so the cost is
            # proportional to movement rather than to the number of folios.
            if previous > 0:
                receivables -= previous
            elif previous < 0:
                deposits += previous
            if current > 0:
                receivables += current
            elif current < 0:
                deposits -= current

        by_date[business_date] = {
            "ar_created": created,
            "ar_settled": settled,
            "ar_closing": receivables,
            "guest_credit_closing": deposits,
        }

    return {
        "opening": receivables,
        "deposits_opening": deposits,
        "by_date": by_date,
    }


def collection_delta():
    return {
        "ar_created": 0,
        "ar_settled": 0,
        "ar_closing": 0,
        "guest_credit_closing": 0,
    }


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------
def build_date_row(
    business_date,
    occupancy,
    arrivals,
    money,
    modes,
    ar,
    ar_opening,
    guest_credit_opening,
    include_zero_rows,
):
    if not include_zero_rows and not row_has_activity(occupancy, arrivals, money):
        return None

    row = {
        "business_date": business_date,
        "rooms_inventory": occupancy["rooms_inventory"],
        "rooms_out_of_order": occupancy["rooms_out_of_order"],
        "rooms_available": occupancy["rooms_available"],
        "room_nights_sold": occupancy["room_nights_sold"],
        "room_nights_complimentary": occupancy["room_nights_complimentary"],
        "room_nights_house_use": occupancy["room_nights_house_use"],
        "day_use": occupancy["day_use"],
        "occupancy_pct": occupancy["occupancy_pct"],
        "arrivals": arrivals["arrivals"],
        "departures": arrivals["departures"],
        "in_house": arrivals["in_house"],
        "no_shows": arrivals["no_shows"],
    }

    row.update(build_money_columns(money, modes))
    row.update(build_ar_columns(ar, ar_opening, guest_credit_opening))
    row["revenue_recognised"] = row["total_revenue_gross"]
    row["difference"] = row["revenue_recognised"] - row["total_collected"]

    nights = row["room_nights_sold"]
    available = row["rooms_available"]
    row["adr"] = row["room_revenue_gross"] / nights if nights else 0
    row["revpar"] = row["room_revenue_gross"] / available if available else 0

    return row


def build_channel_row(business_date, channel, money, modes, include_zero_rows):
    if not include_zero_rows and not row_has_activity(None, None, money):
        return None

    row = {"business_date": business_date, "channel": channel}
    row.update(build_money_columns(money, modes))
    row["revenue_recognised"] = row["total_revenue_gross"]
    row["difference"] = row["revenue_recognised"] - row["total_collected"]
    return row


def build_money_columns(money, modes):
    row = {
        "room_revenue_nett": money.get("room_revenue_nett", 0),
        "room_revenue_tax_service": money.get("room_revenue_tax_service", 0),
        "room_revenue_gross": money.get("room_revenue_gross", 0),
        "breakfast_nett": money.get("breakfast_nett", 0),
        "breakfast_tax_service": money.get("breakfast_tax_service", 0),
        "breakfast_gross": money.get("breakfast_gross", 0),
        "restaurant_revenue": money.get(LINE_RESTAURANT, 0),
        "room_service_revenue": money.get(LINE_ROOM_SERVICE, 0),
        "banquet_revenue": money.get(LINE_BANQUET, 0),
        "laundry_revenue": money.get(LINE_LAUNDRY, 0),
        "other_revenue": money.get(LINE_OTHER, 0),
        "commission": money.get("commission", 0),
        "total_collected": money.get("total_collected", 0),
    }

    row["total_revenue_gross"] = sum(
        row[field]
        for field in (
            "room_revenue_gross",
            "breakfast_gross",
            "restaurant_revenue",
            "room_service_revenue",
            "banquet_revenue",
            "laundry_revenue",
            "other_revenue",
        )
    )
    row["net_revenue_after_commission"] = row["total_revenue_gross"] - row["commission"]

    collected_modes = money.get("modes") or {}
    for mode in modes:
        row[mode_fieldname(mode)] = collected_modes.get(mode, 0)
    row["cash_collected"] = collected_modes.get("Cash", 0)

    return row


def build_ar_columns(ar, ar_opening, guest_credit_opening=None):
    return {
        "ar_opening": ar_opening,
        "guest_credit_opening": guest_credit_opening,
        "ar_created": ar.get("ar_created", 0),
        "ar_settled": ar.get("ar_settled", 0),
        "ar_closing": ar.get("ar_closing", 0),
        "guest_credit_closing": ar.get("guest_credit_closing", 0),
    }


def row_has_activity(occupancy, arrivals, money):
    if money and (money.get("total_collected") or money.get("modes")):
        return True
    if occupancy and (
        occupancy["room_nights_sold"]
        or occupancy["room_nights_complimentary"]
        or occupancy["room_nights_house_use"]
    ):
        return True
    if arrivals and (arrivals["arrivals"] or arrivals["departures"] or arrivals["in_house"]):
        return True
    return False


# ---------------------------------------------------------------------------
# Totals and summary
# ---------------------------------------------------------------------------
def new_totals():
    return {
        "room_revenue_gross": 0,
        "total_revenue_gross": 0,
        "total_collected": 0,
        "commission": 0,
        "ar_closing": 0,
        "guest_credit_closing": 0,
        "room_nights_sold": 0,
        "rooms_available": 0,
        "modes": {},
    }


def accumulate_date_totals(totals, row, modes):
    totals["room_revenue_gross"] += row["room_revenue_gross"]
    totals["total_revenue_gross"] += row["total_revenue_gross"]
    totals["total_collected"] += row["total_collected"]
    totals["commission"] += row["commission"]
    totals["room_nights_sold"] += row["room_nights_sold"]
    totals["rooms_available"] += row["rooms_available"]
    for mode in modes:
        totals["modes"][mode] = totals["modes"].get(mode, 0) + row.get(mode_fieldname(mode), 0)
    # Closing balances are point-in-time, so the last date wins.
    totals["ar_closing"] = row["ar_closing"]
    totals["guest_credit_closing"] = row["guest_credit_closing"]


def accumulate_channel_totals(totals, row, modes):
    totals["room_revenue_gross"] += row["room_revenue_gross"]
    totals["total_revenue_gross"] += row["total_revenue_gross"]
    totals["total_collected"] += row["total_collected"]
    totals["commission"] += row["commission"]
    for mode in modes:
        totals["modes"][mode] = totals["modes"].get(mode, 0) + row.get(mode_fieldname(mode), 0)


def build_report_summary(totals, occupancy):
    nights = totals["room_nights_sold"]
    available = totals["rooms_available"]

    return [
        {
            "label": _("Room Revenue"),
            "value": totals["room_revenue_gross"],
            "datatype": "Currency",
            "indicator": "Blue",
        },
        {
            "label": _("Total Revenue"),
            "value": totals["total_revenue_gross"],
            "datatype": "Currency",
            "indicator": "Green",
        },
        {
            "label": _("Total Collected"),
            "value": totals["total_collected"],
            "datatype": "Currency",
            "indicator": "Purple",
        },
        {
            "label": _("Recognised - Collected"),
            "value": totals["total_revenue_gross"] - totals["total_collected"],
            "datatype": "Currency",
            "indicator": "Orange",
        },
        {
            "label": _("Occupancy %"),
            "value": round(100.0 * nights / available, 2) if available else 0,
            "datatype": "Percent",
            "indicator": "Blue",
        },
        {
            "label": _("ADR"),
            "value": totals["room_revenue_gross"] / nights if nights else 0,
            "datatype": "Currency",
            "indicator": "Blue",
        },
        {
            "label": _("RevPAR"),
            "value": totals["room_revenue_gross"] / available if available else 0,
            "datatype": "Currency",
            "indicator": "Blue",
        },
        {
            "label": _("Receivable Closing"),
            "value": totals["ar_closing"],
            "datatype": "Currency",
            "indicator": "Red",
        },
    ]


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_default_dates():
    """Return the last closed business date, for the report's default filters."""
    last = get_last_audit_date()
    return {"from_date": last, "to_date": last}
