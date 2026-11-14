# -*- coding: utf-8 -*-
# Copyright (c) 2026, Core Initiative and contributors
# For license information, please see license.txt

"""Repair Room Booking segments that the check-in sync never reached.

frappe's list-query guard rejects `order_by` naming a column called `start`
(sqlparse types the bare word as a DML keyword), and
`inn_room_booking.update_by_reservation()` used `order_by='start asc, creation
asc'`. So on the affected releases every check-in, check-out and reservation
save threw "Illegal SQL query" before it could reach
`sync_booking_segments_with_reservation`, and the segment stayed 'Booked'.

On Jakarta, 2026-11-14: 38 of the 50 In-House reservations had no 'Stayed'
segment in their current room. The code fix stops new drift; this patch repairs
the drift already in the database.

Usage -- propose, then apply:

    bench --site <site> execute inn.patches.v1_2.fix_unsynced_room_booking_segments.execute
    bench --site <site> execute inn.patches.v1_2.fix_unsynced_room_booking_segments.execute \
        --kwargs '{"dry_run": false}'

It is registered in patches.txt, so `bench migrate` runs it too -- in
propose-only mode, deliberately. This repair moves room-nights and occupancy in
the front-desk reports, so it is not applied as a side effect of a migration.
Compare the sibling patch `fix_overlapping_room_move_bookings`, which applies on
migrate; this one is stricter because its output is report-visible.

Dry run performs no writes at all and therefore does not roll anything back. It
prints a table fingerprint before and after so the proposal can be shown to be
read-only.
"""

from __future__ import unicode_literals

import argparse

import frappe
from frappe.utils import getdate

BOOKING_TABLE = "tabInn Room Booking"

# The statuses sync_booking_segments_with_reservation acts on. 'Cancel' and
# 'No Show' also drift (their segments should all be Canceled) but they do not
# block the desk or move the reports, so they are counted and left alone.
IN_HOUSE = "In House"
FINISH = "Finish"
SYNCED_STATUSES = (IN_HOUSE, FINISH)
COUNTED_STATUSES = ("Cancel", "No Show")

# Every 'Finish' reservation is in scope by status alone, which on Jakarta is
# 24,094 rows going back to 2024. The defect only started with the release that
# carried the list-query guard, and the data agrees: 41 of the 424 November
# checkouts have no 'Finished' segment, against 1 for October and 13 across
# June-October together. So 'Finish' is bounded by departure date and 'In House'
# is not -- the guests in the building now are the ones this exists for.
DEFAULT_SINCE = "2026-11-01"

DATE_FIELDS = ("start", "end")


def plan_unsynced_segment_repair(reservation, segments):
    """Plan the changes that bring one reservation's segments in line with it.

    Pure: `reservation` and `segments` are dicts, `segments` ordered oldest
    first. Returns a list of change dicts.

    The rules mirror inn_room_booking.sync_booking_segments_with_reservation so
    the repair cannot drift from the live code:

      * 'In House', one segment -> start=arrival, end=departure,
                                   room_id=actual_room_id, status='Stayed'
      * 'In House', many        -> arrival to the first non-canceled segment;
                                   departure, room and status to the last.
                                   Intermediate move boundaries are untouched.
      * 'Finish'                -> last non-canceled segment 'Finished' with
                                   end=departure

    Only values that actually differ are proposed, so an empty plan means the
    reservation is already in sync. A reservation whose segments are all
    'Canceled' is proposed nothing: re-activating one is a judgement call, not
    a bulk repair, so the caller reports those for review instead.
    """
    changes = []
    status = reservation.get("status")
    if status not in SYNCED_STATUSES:
        return changes

    non_canceled = [s for s in segments if s.get("status") != "Canceled"]
    if not non_canceled:
        return changes

    if status == "In House":
        if len(non_canceled) == 1:
            changes += propose_changes(non_canceled[0], {
                "start": reservation.get("arrival"),
                "end": reservation.get("departure"),
                "room_id": reservation.get("actual_room_id"),
                "status": "Stayed",
            }, "in_house_single_segment")
            return changes

        changes += propose_changes(non_canceled[0], {
            "start": reservation.get("arrival"),
        }, "in_house_arrival_on_first_segment")
        changes += propose_changes(non_canceled[-1], {
            "end": reservation.get("departure"),
            "room_id": reservation.get("actual_room_id"),
            "status": "Stayed",
        }, "in_house_departure_on_last_segment")
        return changes

    # 'Finish'
    changes += propose_changes(non_canceled[-1], {
        "end": reservation.get("departure"),
        "status": "Finished",
    }, "finish_last_segment")
    return changes


def propose_changes(segment, wanted, reason):
    """One change per field whose current value differs from the wanted one.

    Date fields are emitted as dates, not datetimes: `start` and `end` are DATE
    columns, and a reservation's arrival or departure carries a time. Writing
    the date is exact, and it keeps the proposal readable.
    """
    changes = []
    for field, new_value in wanted.items():
        if new_value in (None, ""):
            continue
        if field in DATE_FIELDS:
            new_value = str(getdate(new_value))
        old_value = segment.get(field)
        if same_value(field, old_value, new_value):
            continue
        changes.append({
            "name": segment.get("name"),
            "reservation_id": segment.get("reference_name"),
            "room_id": segment.get("room_id"),
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
        })
    return changes


def same_value(field, old_value, new_value):
    """Dates compare as dates; every other field compares as text."""
    if field in DATE_FIELDS:
        return getdate(old_value) == getdate(new_value)
    return str(old_value if old_value is not None else "") == str(new_value if new_value is not None else "")


def table_fingerprint():
    """Row count and storage checksum, to show a dry run changed nothing."""
    rows = frappe.db.sql("CHECKSUM TABLE `{0}`".format(BOOKING_TABLE))
    return {
        "rows": frappe.db.count("Inn Room Booking"),
        "checksum": rows[0][1] if rows else None,
    }


def collect_plans(since=DEFAULT_SINCE):
    """Plan every repairable reservation on this site.

    In scope: every 'In House' reservation, and every 'Finish' one departing on
    or after `since`. See DEFAULT_SINCE for why the second is bounded.

    The segments are fetched here rather than through
    inn_room_booking.get_booking_segments_ordered(), because this patch has to
    run against the release it repairs, and that release predates the helper.
    The ordering is the same one the live code uses -- start, then creation,
    with NULL first as MySQL sorts it -- so the planner sees what the sync sees.
    Raw SQL is also the guard-proof route: ordering by `start` through
    frappe.get_all is exactly what this defect is about.
    """
    reservations = frappe.db.sql(
        """select name, status, arrival, departure, actual_room_id
           from `tabInn Reservation`
           where status = %s or (status = %s and departure >= %s)
           order by name""",
        (IN_HOUSE, FINISH, since),
        as_dict=True,
    )

    names = [reservation.name for reservation in reservations]
    segments_by_reservation = {}
    if names:
        rows = frappe.db.sql(
            """select name, reference_name, room_id, start, end, status, creation
               from `tabInn Room Booking`
               where reference_type = 'Inn Reservation' and reference_name in %s
               order by reference_name, start, creation""",
            (names,),
            as_dict=True,
        )
        for row in rows:
            segments_by_reservation.setdefault(row.reference_name, []).append(row)

    changes = []
    already_in_sync = []
    no_segments = []
    all_canceled = []

    for reservation in reservations:
        segments = segments_by_reservation.get(reservation.name, [])

        if not segments:
            no_segments.append(reservation.name)
            continue

        if all(s.get("status") == "Canceled" for s in segments):
            all_canceled.append(reservation.name)
            continue

        planned = plan_unsynced_segment_repair(reservation, segments)
        if planned:
            changes += planned
        else:
            already_in_sync.append(reservation.name)

    return {
        "reservations": reservations,
        "changes": changes,
        "already_in_sync": already_in_sync,
        "no_segments": no_segments,
        "all_canceled": all_canceled,
    }


def count_other_drift():
    """Reservations in the counted-but-untouched statuses that also drifted."""
    rows = frappe.db.sql(
        """select r.name
           from `tabInn Reservation` r
           join `tabInn Room Booking` b
             on b.reference_type = 'Inn Reservation' and b.reference_name = r.name
           where r.status in %s and b.status != 'Canceled'
           group by r.name""",
        (COUNTED_STATUSES,),
        as_dict=True,
    )
    return [r.name for r in rows]


def print_table(rows, columns):
    widths = []
    for column in columns:
        longest = max([len(str(row.get(column, ""))) for row in rows] or [0])
        widths.append(max(len(column), longest))
    line = "  ".join("{0:<{1}}".format(column, width) for column, width in zip(columns, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(
            "{0:<{1}}".format(str(row.get(column, ""))[:width], width)
            for column, width in zip(columns, widths)))


def count_finish_before(since):
    """Finish reservations the departure window leaves out, so the exclusion is
    visible in the output rather than implied."""
    rows = frappe.db.sql(
        """select count(*) from `tabInn Reservation`
           where status = %s and (departure is null or departure < %s)""",
        (FINISH, since),
    )
    return rows[0][0] if rows else 0


def execute(dry_run=True, since=DEFAULT_SINCE):
    """Propose the repair, and apply it only when asked.

    `dry_run` is the safety default: `bench migrate` picks it up through
    patches.txt and this patch proposes nothing on a migration.
    """
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")
    dry_run = bool(dry_run)

    print("=" * 100)
    print("Repair unsynced Room Booking segments (dry_run={0})".format(dry_run))
    print("  in scope: every In House reservation, plus Finish departing on or after {0}".format(since))
    print("=" * 100)

    before = table_fingerprint()
    print("before: {0} rows, checksum {1}".format(before["rows"], before["checksum"]))

    planned = collect_plans(since)
    changes = planned["changes"]

    print("\nscanned {0} reservations".format(len(planned["reservations"])))
    print("  already in sync        : {0}".format(len(planned["already_in_sync"])))
    print("  need repair            : {0} reservations, {1} field changes".format(
        len({c["reservation_id"] for c in changes}), len(changes)))
    print("  no segments at all     : {0}{1}".format(
        len(planned["no_segments"]),
        "  (not repairable here -- a move now reports these)" if planned["no_segments"] else ""))
    print("  all segments Canceled  : {0}{1}".format(
        len(planned["all_canceled"]),
        "  (left alone: re-activating one is a judgement call)" if planned["all_canceled"] else ""))
    if planned["no_segments"]:
        print("    {0}".format(", ".join(planned["no_segments"][:20])
                             + (" ..." if len(planned["no_segments"]) > 20 else "")))
    if planned["all_canceled"]:
        print("    {0}".format(", ".join(planned["all_canceled"][:20])
                             + (" ..." if len(planned["all_canceled"]) > 20 else "")))

    other = count_other_drift()
    print("\ncounted, not touched: {0} reservations with status in {1} hold a "
          "non-canceled segment".format(len(other), ", ".join(COUNTED_STATUSES)))
    print("counted, not touched: {0} Finish reservations departed before {1}".format(
        count_finish_before(since), since))

    if changes:
        print("\nproposed changes:")
        print_table(changes, ["reservation_id", "name", "room_id", "field", "old_value", "new_value", "reason"])
        print("\nby reason:")
        for reason in sorted({c["reason"] for c in changes}):
            print("  {0:<38} {1}".format(reason, sum(1 for c in changes if c["reason"] == reason)))
    else:
        print("\nno changes needed; every segment already matches its reservation")

    if dry_run:
        print("\n[DRY RUN] no writes issued.")
    elif changes:
        print("\napplying {0} updates to `{1}`...".format(len(changes), BOOKING_TABLE))
        for change in changes:
            frappe.db.sql(
                "UPDATE `{0}` SET `{1}` = %s WHERE name = %s".format(BOOKING_TABLE, change["field"]),
                (change["new_value"], change["name"]),
            )
        frappe.db.commit()
        print("committed {0} updates.".format(len(changes)))
    else:
        print("\nnothing to apply.")

    after = table_fingerprint()
    print("\nafter:  {0} rows, checksum {1}".format(after["rows"], after["checksum"]))
    if dry_run and before != after:
        print("[WARNING] the fingerprint changed on a dry run; investigate before applying")

    return {
        "dry_run": dry_run,
        "since": since,
        "reservations_scanned": len(planned["reservations"]),
        "changes_count": len(changes),
        "reservations_needing_repair": len({c["reservation_id"] for c in changes}),
        "already_in_sync": len(planned["already_in_sync"]),
        "no_segments": planned["no_segments"],
        "all_canceled": planned["all_canceled"],
        "other_drift_counted": other,
        "finish_before_window": count_finish_before(since),
        "fingerprint_before": before,
        "fingerprint_after": after,
        "changes": changes,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair unsynced Room Booking segments.")
    parser.add_argument("--apply", action="store_true", default=False,
                        help="write the changes; without it the patch only proposes")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help="earliest departure date for Finish reservations (default {0})".format(DEFAULT_SINCE))
    args = parser.parse_args()
    execute(dry_run=not args.apply, since=args.since)
