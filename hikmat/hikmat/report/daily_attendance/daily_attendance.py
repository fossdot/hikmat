# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Daily Attendance — one row per (student × date) in the range: active minutes on
the learning devices and a Present/Absent mark against the configurable threshold
(Hikmat Settings → attendance_min_minutes, default 150 = 2.5 hours).

A student with NO Attendance Day row for a date is an explicit Absent row — a
missing record is exactly what an attendance report exists to surface. Present is
recomputed here from active_secs, so editing the threshold reclassifies history;
the stored `present` check is only a list-view convenience snapshot. No weekend or
holiday logic: the NGO's schedule is the facilitator's context, not the report's.
"""
from datetime import timedelta

import frappe
from frappe import _

MAX_RANGE_DAYS = 62  # keep roster × dates sane


def execute(filters=None):
    filters = frappe._dict(filters or {})
    from_date = frappe.utils.getdate(filters.get("from_date") or frappe.utils.nowdate())
    to_date = frappe.utils.getdate(filters.get("to_date") or frappe.utils.nowdate())
    if to_date < from_date:
        from_date, to_date = to_date, from_date
    if (to_date - from_date).days > MAX_RANGE_DAYS:
        from_date = to_date - timedelta(days=MAX_RANGE_DAYS)

    min_secs = (frappe.utils.cint(
        frappe.db.get_single_value("Hikmat Settings", "attendance_min_minutes")) or 150) * 60

    sfilters = {"active": 1}
    if filters.get("campus"):
        sfilters["campus"] = filters.campus
    if filters.get("cohort"):
        sfilters["cohort"] = filters.cohort
    if filters.get("student"):
        sfilters["name"] = filters.student
    roster = frappe.get_all("Student", filters=sfilters,
                            fields=["name", "student_name", "cohort", "campus"],
                            order_by="student_name asc")

    days = {}
    if roster:
        for d in frappe.get_all(
                "Attendance Day",
                filters={"student": ["in", [s.name for s in roster]],
                         "date": ["between", [from_date, to_date]]},
                fields=["student", "date", "active_secs", "device_count",
                        "first_ping", "last_ping"]):
            days[(d.student, str(d.date))] = d

    rows = []
    dates = [from_date + timedelta(days=i) for i in range((to_date - from_date).days + 1)]
    for s in roster:
        for date in dates:
            d = days.get((s.name, str(date)))
            secs = d.active_secs if d else 0
            present = secs >= min_secs
            if filters.get("hide_absent") and not present:
                continue
            rows.append({
                "student": s.name, "student_name": s.student_name,
                "cohort": s.cohort, "campus": s.campus, "date": date,
                "active_minutes": round(secs / 60),
                "status": "Present" if present else "Absent",
                "first_ping": d.first_ping if d else None,
                "last_ping": d.last_ping if d else None,
                "device_count": d.device_count if d else 0,
            })

    columns = [
        {"fieldname": "student", "label": _("Student"), "fieldtype": "Link", "options": "Student", "width": 120},
        {"fieldname": "student_name", "label": _("Name"), "fieldtype": "Data", "width": 130},
        {"fieldname": "cohort", "label": _("Cohort"), "fieldtype": "Link", "options": "Cohort", "width": 110},
        {"fieldname": "campus", "label": _("Campus"), "fieldtype": "Link", "options": "Campus", "width": 110},
        {"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
        {"fieldname": "active_minutes", "label": _("Active min"), "fieldtype": "Int", "width": 100},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
        {"fieldname": "first_ping", "label": _("First seen"), "fieldtype": "Datetime", "width": 150},
        {"fieldname": "last_ping", "label": _("Last seen"), "fieldtype": "Datetime", "width": 150},
        {"fieldname": "device_count", "label": _("Devices"), "fieldtype": "Int", "width": 80},
    ]

    present_by_date = {}
    for r in rows:
        if r["status"] == "Present":
            present_by_date[str(r["date"])] = present_by_date.get(str(r["date"]), 0) + 1
    chart = {
        "data": {
            "labels": [str(d) for d in dates],
            "datasets": [{"name": _("Present"),
                          "values": [present_by_date.get(str(d), 0) for d in dates]}],
        },
        "type": "bar",
        "colors": ["#6c5ce7"],
    } if rows else None

    total = len(rows) if not filters.get("hide_absent") else len(roster) * len(dates)
    present_days = sum(1 for r in rows if r["status"] == "Present")
    active_minutes = [r["active_minutes"] for r in rows if r["status"] == "Present"]
    report_summary = [
        {"label": _("Present days"), "value": present_days, "datatype": "Int", "indicator": "Green"},
        {"label": _("Absent days"), "value": max(0, total - present_days), "datatype": "Int",
         "indicator": "Red" if total - present_days else "Green"},
        {"label": _("Attendance %"),
         "value": (str(round(100 * present_days / total)) + "%") if total else "—", "datatype": "Data"},
        {"label": _("Avg active min (present days)"),
         "value": round(sum(active_minutes) / len(active_minutes)) if active_minutes else 0,
         "datatype": "Int"},
    ]

    return columns, rows, None, chart, report_summary
