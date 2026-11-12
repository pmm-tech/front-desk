# -*- coding: utf-8 -*-
# Copyright (c) 2026, Core Initiative and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import argparse
import sys
import frappe
from frappe.utils import getdate


def plan_room_booking_repair(bookings_by_res, moves):
    """Pure helper that plans the repairs for room move overlaps.

    Returns a list of change dictionaries:
    [{
        'name': booking_name,
        'reservation_id': res_id,
        'room_id': room_id,
        'field': 'end' | 'start' | 'status',
        'old_value': ...,
        'new_value': ...,
        'reason': 'mid_stay_truncation' | 'same_day_cancellation' | 'start_date_restoration'
    }]
    """
    changes = []
    processed_bookings = set()

    # Pass 1: Correlate with historical Inn Move Room records
    for move in moves:
        res_id = move.get("reservation_id")
        old_room = move.get("old_room_id")
        new_room = move.get("new_room")
        move_date_raw = move.get("arrival") or move.get("creation")
        if not move_date_raw:
            continue
        move_date = getdate(move_date_raw)

        res_bookings = bookings_by_res.get(res_id, [])

        # Check old room bookings
        for b in res_bookings:
            if b.get("room_id") == old_room and b.get("status") != "Canceled":
                b_start = getdate(b.get("start"))
                b_end = getdate(b.get("end"))

                if b_start <= move_date < b_end:
                    if move_date == b_start:
                        # Same-day move: guest spent 0 nights in old room
                        if b.get("status") != "Canceled":
                            changes.append({
                                "name": b.get("name"),
                                "reservation_id": res_id,
                                "room_id": old_room,
                                "field": "status",
                                "old_value": b.get("status"),
                                "new_value": "Canceled",
                                "reason": "same_day_cancellation",
                            })
                            b["status"] = "Canceled"
                        if b_end != move_date:
                            changes.append({
                                "name": b.get("name"),
                                "reservation_id": res_id,
                                "room_id": old_room,
                                "field": "end",
                                "old_value": str(b_end),
                                "new_value": str(move_date),
                                "reason": "same_day_cancellation",
                            })
                            b["end"] = move_date
                    else:
                        # Mid-stay move: truncate end to move_date
                        if b.get("status") != "Finished":
                            changes.append({
                                "name": b.get("name"),
                                "reservation_id": res_id,
                                "room_id": old_room,
                                "field": "status",
                                "old_value": b.get("status"),
                                "new_value": "Finished",
                                "reason": "mid_stay_truncation",
                            })
                            b["status"] = "Finished"
                        if b_end != move_date:
                            changes.append({
                                "name": b.get("name"),
                                "reservation_id": res_id,
                                "room_id": old_room,
                                "field": "end",
                                "old_value": str(b_end),
                                "new_value": str(move_date),
                                "reason": "mid_stay_truncation",
                            })
                            b["end"] = move_date

                    processed_bookings.add(b.get("name"))

        # Check new room bookings (restore start date if backdated to reservation arrival)
        for b in res_bookings:
            if b.get("room_id") == new_room and b.get("status") != "Canceled":
                b_start = getdate(b.get("start"))
                if b_start < move_date:
                    changes.append({
                        "name": b.get("name"),
                        "reservation_id": res_id,
                        "room_id": new_room,
                        "field": "start",
                        "old_value": str(b_start),
                        "new_value": str(move_date),
                        "reason": "start_date_restoration",
                    })
                    b["start"] = move_date
                    processed_bookings.add(b.get("name"))

    # Pass 2: Secondary sweep for any remaining multi-segment overlaps
    for res_id, res_bookings in bookings_by_res.items():
        active_segments = [
            b for b in res_bookings
            if b.get("room_availability") == "Room Sold" and b.get("status") in ("Stayed", "Finished")
        ]
        if len(active_segments) <= 1:
            continue

        # Sort segments chronologically
        active_segments.sort(key=lambda x: (getdate(x.get("start")), str(x.get("creation", ""))))

        for i in range(len(active_segments) - 1):
            cur = active_segments[i]
            nxt = active_segments[i + 1]

            cur_start = getdate(cur.get("start"))
            cur_end = getdate(cur.get("end"))
            nxt_start = getdate(nxt.get("start"))

            if cur_end > nxt_start:
                if cur_start == nxt_start:
                    # Same-day overlap: cancel earlier segment
                    if cur.get("status") != "Canceled":
                        changes.append({
                            "name": cur.get("name"),
                            "reservation_id": res_id,
                            "room_id": cur.get("room_id"),
                            "field": "status",
                            "old_value": cur.get("status"),
                            "new_value": "Canceled",
                            "reason": "same_day_cancellation",
                        })
                        cur["status"] = "Canceled"
                    if cur_end != cur_start:
                        changes.append({
                            "name": cur.get("name"),
                            "reservation_id": res_id,
                            "room_id": cur.get("room_id"),
                            "field": "end",
                            "old_value": str(cur_end),
                            "new_value": str(cur_start),
                            "reason": "same_day_cancellation",
                        })
                        cur["end"] = cur_start
                else:
                    # Mid-stay overlap: truncate cur_end to nxt_start
                    if cur_end != nxt_start:
                        changes.append({
                            "name": cur.get("name"),
                            "reservation_id": res_id,
                            "room_id": cur.get("room_id"),
                            "field": "end",
                            "old_value": str(cur_end),
                            "new_value": str(nxt_start),
                            "reason": "mid_stay_truncation",
                        })
                        cur["end"] = nxt_start

    return changes


def execute(dry_run=False):
    """Repair historical overlapping room move bookings.

    Can be run via:
    - bench migrate (dry_run=False)
    - bench execute inn.patches.v1_2.fix_overlapping_room_move_bookings.execute --kwargs '{"dry_run": True}'
    """
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")

    print("\n" + "=" * 80)
    print("Repair Overlapping Room Move Bookings (dry_run={0})".format(dry_run))
    print("=" * 80)

    # 1. Fetch all Inn Move Room records
    moves = frappe.db.sql("""
        SELECT name, reservation_id, old_room_id, new_room, arrival, departure, creation
        FROM `tabInn Move Room`
        ORDER BY creation ASC
    """, as_dict=True)

    print("Found {0} Inn Move Room records to analyze.".format(len(moves)))

    # 2. Fetch all Room Bookings for reservations that have moves or multiple segments
    bookings_raw = frappe.db.sql("""
        SELECT name, reference_name, room_id, start, end, room_availability, status, creation
        FROM `tabInn Room Booking`
        WHERE reference_type = 'Inn Reservation'
        ORDER BY start ASC, creation ASC
    """, as_dict=True)

    bookings_by_res = {}
    for b in bookings_raw:
        res_id = b.get("reference_name")
        if res_id:
            bookings_by_res.setdefault(res_id, []).append(b)

    # 3. Plan repairs
    changes = plan_room_booking_repair(bookings_by_res, moves)

    print("Identified {0} booking updates needed across {1} distinct reservations.".format(
        len(changes),
        len(set(c["reservation_id"] for c in changes))
    ))

    if not changes:
        print("No overlapping room bookings found. Database is clean.")
        return {"dry_run": dry_run, "changes_count": 0, "changes": []}

    # Print summary table
    print("\n{:<15} {:<12} {:<10} {:<8} {:<15} {:<15} {:<22}".format(
        "Booking", "Reservation", "Room", "Field", "Old Value", "New Value", "Reason"
    ))
    print("-" * 100)
    for c in changes:
        print("{:<15} {:<12} {:<10} {:<8} {:<15} {:<15} {:<22}".format(
            c["name"], c["reservation_id"], c["room_id"], c["field"],
            str(c["old_value"]), str(c["new_value"]), c["reason"]
        ))
    print("-" * 100)

    if dry_run:
        print("\n[DRY RUN] No changes were written to the database.")
        frappe.db.rollback()
    else:
        print("\nApplying updates to `tabInn Room Booking`...")
        for c in changes:
            frappe.db.sql("""
                UPDATE `tabInn Room Booking`
                SET `{0}` = %s
                WHERE name = %s
            """.format(c["field"]), (c["new_value"], c["name"]))

        frappe.db.commit()
        print("Successfully committed {0} updates.".format(len(changes)))

    return {
        "dry_run": dry_run,
        "changes_count": len(changes),
        "changes": changes,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair overlapping room move bookings.")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Inspect changes without committing")
    args = parser.parse_args()

    execute(dry_run=args.dry_run)
