"""Collapse the routes a feed lists more than once.

Measured against production on 2026-10-08, the duplication is two different problems that one label
hid, and treating them the same would destroy real information:

* **Brisbane**: 728 of 1115 route entries repeat both their short *and* long name — thirteen copies
  of "BRBD · Brisbane City - Airport", identical in every way a rider can see. Feed bookkeeping.
* **Boston**: 205 entries repeat a short name and *none* repeat the long one. The thirty-eight
  "Red Line Shuttle" rows are thirty-eight different shuttles — Broadway to JFK, Ashmont to JFK,
  Quincy Center to Broadway. Merging them would tell a rider one bus goes everywhere.

So the rule merges only on an exact match of what a rider is shown, and the second problem is left
to the client, which has the long name and simply was not showing it.

Roma, Santiago, Toronto and Kuala Lumpur have no duplicates at all; for them this is a no-op.
"""
from __future__ import annotations


def _key(r: dict) -> tuple:
    """What has to match before two rows are the same route to a rider.

    Component is in the key because a feeder and a trunk sharing a number are not the same service,
    and the app already colours them differently — merging across that would make the colour a lie.
    """
    return (
        r.get("component"),
        (r.get("shortName") or "").strip(),
        (r.get("longName") or "").strip(),
        r.get("mode"),
    )


def merge_duplicate_routes(routes: list[dict]) -> list[dict]:
    """One entry per distinct (component, short name, long name, mode), order preserved.

    The surviving entry keeps the lowest id of its group, so the choice is stable across ingests and
    two runs of the same feed produce the same list. Every id that was merged away is listed in
    `mergedIds`, because a deep link to one of them must keep working and a client showing "6
    variants" needs to know there were six.

    A route with no short name and no long name is never merged: there is nothing to compare, and
    collapsing on component alone would fold a whole network into one row.
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    out: list[dict] = []
    for r in routes:
        k = _key(r)
        if not k[1] and not k[2]:
            out.append(r)
            continue
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)

    merged: list[dict] = []
    for k in order:
        rows = sorted(groups[k], key=lambda r: str(r.get("id") or ""))
        head = dict(rows[0])
        if len(rows) > 1:
            head["mergedIds"] = [str(r.get("id")) for r in rows[1:]]
        merged.append(head)

    # Unnamed routes keep their place at the end rather than being dropped or reordered into the
    # middle of a list someone is scrolling.
    return merged + out
