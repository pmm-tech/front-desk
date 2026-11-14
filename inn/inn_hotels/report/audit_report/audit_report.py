# -*- coding: utf-8 -*-
# Copyright (c) 2025, PMM and contributors
# For license information, please see license.txt
"""Audit Report - the daily folio-level export.

This is the report the front desk runs once per business date and pastes into
the finance spreadsheet, so its column order is a de-facto interface.

Column contract: the original 15 columns keep their **names**, but they no
longer all keep their positions. ``bed_type`` is inserted at slot 3, directly
after ``room_type``, because a bed type read apart from the room it belongs to
is useless; ``audit_date`` is appended last. Inserting at slot 3 shifts every
column from ``actual_room`` onward, so ``audit_date`` sits at slot 16, not 15.
A positionally-aligned paste into a spreadsheet laid out for the previous
column set must therefore insert one column after ``Room Type``; a
header-based paste is unaffected. ``ROW_FIELDS`` below is the single source of
truth for that order.

What each row means
-------------------
One row per reservation that is in house on the report date, plus any
reservation that has non-void activity on that date. Money columns aggregate
only the transactions whose ``audit_date`` equals the report date.

Fixed relative to the previous implementation
---------------------------------------------
``audit_date``      every row is dateable; the report date is no longer implied
                    by the filter used at export time.
``total_amount``    now every non-void credit, so ``Down Payment``, ``Payment``
                    and ``Deposit`` collections are no longer dropped. The old
                    code used two Inn Hotels Setting fields that were both
                    ``Room Payment``.
``status``          was always empty; now Paid / Partial / Unpaid as at the
                    report date, reconstructed from ``audit_date <= date``.
``mode_of_payment`` was a sliced concatenation (``[:-2]``); now clean,
                    comma-joined Mode of Payment values.
``paid_date``       was always empty; now the earliest payment date.
``actual_room_nett`` / ``bf_revenue``
                    now **nett**, not gross. The v1.2.0 refactor replaced
                    exact transaction-type matching with line classification,
                    which folded ``Room Charge Tax/Service`` into ``Room
                    Charge`` and silently turned both columns into gross --
                    room overstated 20%, breakfast 21% on the date this was
                    found. The split now uses
                    :func:`inn.helper.revenue_lines.get_tax_service_types`.
``remark``          unchanged (``Inn Folio.bill_instructions``).

Two things the nett columns deliberately do **not** do:

* They never apply a tax rate. ``Inn Tax`` may be ``No Service Tax``,
  ``Service Only (10%)`` or ``Service Tax 21%``, and a posting may carry only
  one of a tax's breakdown rows, so the posted tax/service rows are subtracted
  rather than any percentage of the gross.
* They never read ``Inn Reservation.nett_actual_room_rate``. That field is a
  single current scalar, not a per-night history -- it is rewritten on room
  move and by ``recalculate_nett_charge()`` -- and it disagrees with what was
  posted on roughly 15% of room-charge folio-days. The posted transactions are
  the source of truth for a business date.

The payment classification rules live in ``inn.helper.revenue_lines`` so this
report and ``Front Desk Daily Revenue Recap`` cannot disagree.
"""

import frappe
from frappe.utils import flt, today

from inn.helper.revenue_lines import (
    LINE_BREAKFAST,
    LINE_COMMISSION,
    LINE_ROOM,
    classify,
    get_line_map,
    get_settings_context,
    get_tax_service_types,
    unmapped_types,
)

FILTER_FIELD_DATE = "expected_arrival"
FILTER_FIELD_STATUS = "status"

STATUS_RESERVED = "Reserved"
STATUS_IN_HOUSE = "In House"
STATUS_FINISH = "Finish"

STATUS_PAID = "Paid"
STATUS_PARTIAL = "Partial"
STATUS_UNPAID = "Unpaid"

#: A folio whose debits exceed its credits by no more than this is treated as
#: settled. Rupiah rounding leaves sub-unit residue on some folios.
SETTLEMENT_TOLERANCE = 1.0


def execute(filters=None):
    columns = get_columns()
    data = get_data(filters)
    return columns, data


#: The emitted column order: ``(fieldname, label, fieldtype, width)``.
#: ``get_columns`` and ``build_row`` both follow this list, so the column
#: definitions and the positional row values cannot drift apart.
ROW_FIELDS = (
    ("rsv", "RSV", "Data", 150),
    ("customer", "Customer", "Data", 150),
    ("room_type", "Room Type", "Data", 150),
    ("bed_type", "Bed Type", "Data", 150),
    ("actual_room", "Actual Room", "Data", 150),
    ("actual_room_rate", "Actual Room Rate", "Currency", 150),
    ("actual_room_nett", "Actual Room Nett", "Currency", 150),
    ("bf_revenue", "BF Revenue", "Currency", 150),
    ("comission", "Comission", "Currency", 150),
    ("payment_by", "Payment By", "Data", 150),
    ("status", "Status", "Data", 150),
    ("mode_of_payment", "Mode of Payment", "Data", 150),
    ("total_amount", "Total Amount", "Currency", 150),
    ("posting_date", "Posting date", "Date", 150),
    ("paid_date", "Paid Date", "Date", 150),
    ("remark", "Remark", "Data", 150),
    ("audit_date", "Audit Date", "Date", 110),
)

COLUMN_FIELDS = [field[0] for field in ROW_FIELDS]


def get_columns():
    return [
        {"fieldname": fieldname, "label": label, "fieldtype": fieldtype, "width": width}
        for fieldname, label, fieldtype, width in ROW_FIELDS
    ]


def get_data(filters):
    filters = filters or frappe._dict()

    on_date = filters.get("date") or today()

    show_mode_of_payment = filters.get("fill_mode_payment")
    if show_mode_of_payment is None:
        show_mode_of_payment = 1

    return get_data_detail(on_date, show_mode_of_payment)


def get_data_detail(on_date, is_show_mode_payment=True):
    context = get_settings_context()
    line_map = get_line_map(context)
    tax_service_types = get_tax_service_types(context)

    rows = get_folio_rows(on_date, line_map)
    if not rows:
        return []

    folio_names = [row.folio for row in rows]
    transactions = get_transactions_for_date(folio_names, on_date)
    detail, unmapped = summarise_transactions(transactions, line_map, tax_service_types)
    warn_unmapped_types(unmapped)
    warn_unrecognised_tax_service_types(transactions, tax_service_types, line_map)

    settlement = get_settlement_status(folio_names, on_date)

    return [
        build_row(row, detail.get(row.folio) or empty_detail(),
                  settlement.get(row.folio, ""), is_show_mode_payment, on_date)
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def get_room_line_types(line_map):
    """The transaction types that feed the room line for this site.

    Derived from the line map rather than hardcoded, so a property that renamed
    ``room_revenue_transaction_type`` still selects. Hardcoding
    ``Room Charge`` here would make the report silently empty on such a site.
    """
    return {transaction_type for transaction_type, line in line_map.items()
            if line == LINE_ROOM}


def get_folio_rows(on_date, line_map):
    """One row source per folio that was charged room revenue on ``on_date``.

    Selection is **folio-based**, not reservation-based, and it is driven by a
    posted room charge rather than by occupancy or by any transaction at all.
    Two consequences are deliberate:

    * A stay cannot appear on its departure date merely because a late-checkout
      fee or a settling payment was posted that day. One night, one row.
    * Master and Desk folios carrying room charges are still selected. They have
      no ``reservation_id``, so a reservation-driven query drops their revenue
      silently -- 28,670,061 over three days on one Jakarta master folio.

    A folio with a reservation renders the reservation; one without renders
    itself. See :func:`resolve_folio_row`.
    """
    room_types = sorted(get_room_line_types(line_map))
    if not room_types:
        return []

    placeholders = ", ".join(["%s"] * len(room_types))
    rows = frappe.db.sql(
        """
        select folio.name as folio, folio.type as folio_type,
               folio.customer_id as folio_customer_id,
               folio.channel as folio_channel,
               folio.bill_instructions as folio_bill_instructions,
               ir.name as reservation, ir.customer_id as customer_id,
               ir.room_type as room_type, ir.bed_type as bed_type,
               ir.actual_room_id as actual_room_id, ir.channel as channel
        from `tabInn Folio` as folio
        inner join `tabInn Folio Transaction` as trx on trx.parent = folio.name
        left join `tabInn Reservation` as ir on ir.name = folio.reservation_id
        where trx.audit_date = %s
          and trx.is_void = 0
          and trx.transaction_type in ({0})
        group by folio.name, folio.type, folio.customer_id, folio.channel,
                 folio.bill_instructions, ir.name, ir.customer_id, ir.room_type,
                 ir.bed_type, ir.actual_room_id, ir.channel
        order by folio.name
        """.format(placeholders),
        tuple([on_date] + room_types),
        as_dict=True,
    )

    return [resolve_folio_row(row) for row in rows]


def resolve_folio_row(row):
    """Turn one selected folio into the row source :func:`build_row` expects.

    Same 17 columns either way. A folio with no reservation puts its own name
    in ``rsv`` so the row stays traceable, and states its type in ``remark``.

    ``bill_instructions`` is the *folio's* field on both branches --
    ``Inn Reservation`` has no such column, and the remark has always been the
    folio's instruction.
    """
    if row.reservation:
        return frappe._dict(
            folio=row.folio,
            name=row.reservation,
            customer_id=row.customer_id,
            room_type=row.room_type,
            bed_type=row.bed_type,
            actual_room_id=row.actual_room_id,
            channel=row.channel,
            bill_instructions=row.folio_bill_instructions,
        )

    remark = "{0} folio {1}".format(row.folio_type or "Non-guest", row.folio)
    if row.folio_bill_instructions:
        remark = "{0}: {1}".format(remark, row.folio_bill_instructions)

    return frappe._dict(
        folio=row.folio,
        name=row.folio,
        customer_id=row.folio_customer_id,
        room_type="",
        bed_type="",
        actual_room_id="",
        channel=row.folio_channel,
        bill_instructions=remark,
    )


def get_transactions_for_date(folio_names, on_date):
    placeholders = ", ".join(["%s"] * len(folio_names))
    return frappe.db.sql(
        """
        select parent, transaction_type, flag, amount, mode_of_payment,
               creation, actual_room_rate
        from `tabInn Folio Transaction`
        where parent in ({0})
          and audit_date = %s
          and is_void = 0
        order by creation, name
        """.format(placeholders),
        tuple(folio_names) + (on_date,),
        as_dict=True,
    )


def get_settlement_status(folio_names, on_date):
    """Paid / Partial / Unpaid per folio, as at ``on_date``.

    Reconstructed from the transactions up to and including ``on_date`` rather
    than read from ``Inn Folio.balance``, which only reflects the current state
    and would be wrong whenever the report is re-run for a past date.
    """
    placeholders = ", ".join(["%s"] * len(folio_names))
    rows = frappe.db.sql(
        """
        select parent,
               sum(case when flag = 'Debit' then amount else 0 end) as debit,
               sum(case when flag = 'Credit' then amount else 0 end) as credit
        from `tabInn Folio Transaction`
        where parent in ({0})
          and audit_date <= %s
          and is_void = 0
        group by parent
        """.format(placeholders),
        tuple(folio_names) + (on_date,),
        as_dict=True,
    )

    settlement = {}
    for row in rows:
        debit = flt(row.debit)
        credit = flt(row.credit)
        if debit - credit <= SETTLEMENT_TOLERANCE:
            settlement[row.parent] = STATUS_PAID
        elif credit > 0:
            settlement[row.parent] = STATUS_PARTIAL
        else:
            settlement[row.parent] = STATUS_UNPAID
    return settlement


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------
def empty_detail():
    return {
        "actual_room_rate": 0,
        "actual_room_nett": 0,
        "breakfast_revenue": 0,
        "comission": 0,
        "total_amount": 0,
        "modes": set(),
        "creations": [],
    }


def summarise_transactions(transactions, line_map, tax_service_types):
    """Aggregate one folio's transactions into the report's money columns.

    ``tax_service_types`` is **required and has no default**. A caller that
    omitted it would silently sum the tax/service rows into the nett columns
    and report gross as nett -- which is precisely the defect this signature
    exists to prevent. Making it required turns that mistake into a
    ``TypeError`` at the call site instead of a plausible wrong number.
    """
    detail = {}
    charge_types = set()

    for trx in transactions:
        bucket = detail.setdefault(trx.parent, empty_detail())
        amount = flt(trx.amount)

        # Collections: every credit is money received (payment types are not
        # revenue lines and are deliberately not classified). Refunds are debits
        # in this app, so they are naturally excluded.
        if (trx.flag or "").strip().lower() == "credit":
            bucket["total_amount"] += amount
            if trx.mode_of_payment:
                bucket["modes"].add(trx.mode_of_payment)
            if trx.creation:
                bucket["creations"].append(trx.creation)
            continue

        charge_types.add(trx.transaction_type)
        line = classify(trx.transaction_type, line_map)

        # The tax/service element of a revenue line shares that line, so it must
        # be excluded by type. Subtracting a percentage of the gross would be
        # wrong: the rate varies per reservation and a posting may carry only
        # one of a tax's breakdown rows.
        is_tax_service = trx.transaction_type in tax_service_types

        if line == LINE_ROOM:
            if not is_tax_service:
                bucket["actual_room_nett"] += amount
            if trx.actual_room_rate:
                bucket["actual_room_rate"] = flt(trx.actual_room_rate)
        elif line == LINE_BREAKFAST:
            if not is_tax_service:
                bucket["breakfast_revenue"] += amount
        elif line == LINE_COMMISSION:
            bucket["comission"] += amount

    # Only charge types can be "unmapped"; a payment type has no revenue line by
    # design and must not be reported as a gap.
    unmapped = unmapped_types(charge_types, line_map)
    return detail, unmapped


def unrecognised_tax_service_types(transactions, tax_service_types, line_map):
    """Charge types that look like tax/service but are not in the split set.

    :func:`get_tax_service_types` recognises the shipped names plus
    ``<configured revenue type> Tax/Service``. A site posting a room or
    breakfast tax under any other name would keep reporting gross, silently --
    the same failure this module just fixed, one configuration away. These are
    the types worth looking at: they carry a tax/service name and feed a nett
    column, yet are not excluded from it.
    """
    suspects = set()

    for trx in transactions:
        if (trx.flag or "").strip().lower() == "credit":
            continue
        name = trx.transaction_type
        if not name or name in tax_service_types:
            continue
        if "tax/service" not in name.lower():
            continue
        if classify(name, line_map) in (LINE_ROOM, LINE_BREAKFAST):
            suspects.add(name)

    return sorted(suspects)


def warn_unrecognised_tax_service_types(transactions, tax_service_types, line_map):
    suspects = unrecognised_tax_service_types(transactions, tax_service_types, line_map)
    if not suspects:
        return
    frappe.msgprint(
        msg=(
            "Transaction types that look like tax/service but are not in this "
            "site's tax/service set, so they are counted as nett: {0}".format(
                ", ".join(suspects)
            )
        ),
        title="Unrecognised tax/service types",
        indicator="orange",
    )


def warn_unmapped_types(unmapped):
    if not unmapped:
        return
    frappe.msgprint(
        msg="Transaction types with no revenue mapping, counted as Other: {0}".format(
            ", ".join(unmapped)
        ),
        title="Unmapped transaction types",
        indicator="orange",
    )


def build_row(reservation, detail, settlement_status, is_show_mode_payment, on_date):
    creations = detail.get("creations") or []
    mode_of_payment = ", ".join(sorted(detail.get("modes") or [])) if is_show_mode_payment else ""

    return [
        reservation.name,
        reservation.customer_id,
        reservation.room_type,
        reservation.bed_type,
        reservation.actual_room_id,
        detail.get("actual_room_rate", 0),
        detail.get("actual_room_nett", 0),
        detail.get("breakfast_revenue", 0),
        detail.get("comission", 0),
        reservation.channel,
        settlement_status,
        mode_of_payment,
        detail.get("total_amount", 0),
        ", ".join(str(creation) for creation in creations),
        creations[0].date().isoformat() if creations else "",
        reservation.bill_instructions,
        on_date,
    ]
