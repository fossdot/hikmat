# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Attendance Summary — one row per student over the range: days present/absent,
attendance %, total active hours. The at-a-glance companion to Daily Attendance
(which is the per-day detail view)."""
from datetime import timedelta

import frappe
from frappe import _

MAX_RANGE_DAYS = 366


def execute(filters=None):
    filters = frappe._dict(filters or {})
    from_date = frappe.utils.getdate(filters.get("from_date") or frappe.utils.nowdate())
    to_date = frappe.utils.getdate(filters.get("to_date") or frappe.utils.nowdate())
    if to_date < from_date:
        from_date, to_date = to_date, from_date
    if (to_date - from_date).days > MAX_RANGE_DAYS:
        from_date = to_date - timedelta(days=MAX_RANGE_DAYS)
    total_days = (to_date - from_date).days + 1

    min_secs = (frappe.utils.cint(
        frappe.db.get_single_value("Hikmat Settings", "attendance_min_minutes")) or 150) * 60

    sfilters = {"active": 1}
    if filters.get("campus"):
        sfilters["campus"] = filters.campus
    if filters.get("cohort"):
        sfilters["cohort"] = filters.cohort
    roster = frappe.get_all("Student", filters=sfilters,
                            fields=["name", "student_name", "cohort", "campus"],
                            order_by="student_name asc")

    agg = {}
    if roster:
        for r in frappe.db.sql(
                """select student,
                          sum(active_secs)                            as secs,
                          sum(case when active_secs >= %s then 1 else 0 end) as present_days,
                          max(last_ping)                              as last_seen
                   from `tabAttendance Day`
                   where student in %s and date between %s and %s
                   group by student""",
                (min_secs, tuple(s.name for s in roster) or ("",), from_date, to_date),
                as_dict=True):
            agg[r.student] = r

    rows = []
    for s in roster:
        a = agg.get(s.name)
        present = int(a.present_days) if a else 0
        secs = int(a.secs) if a else 0
        rows.append({
            "student": s.name, "student_name": s.student_name,
            "cohort": s.cohort, "campus": s.campus,
            "days_present": present,
            "days_absent": total_days - present,
            "attendance_pct": round(100 * present / total_days) if total_days else 0,
            "total_active_hours": round(secs / 3600, 1),
            "avg_minutes_per_day": round(secs / 60 / present) if present else 0,
            "last_seen": a.last_seen if a else None,
        })
    rows.sort(key=lambda r: r["attendance_pct"])

    columns = [
        {"fieldname": "student", "label": _("Student"), "fieldtype": "Link", "options": "Student", "width": 120},
        {"fieldname": "student_name", "label": _("Name"), "fieldtype": "Data", "width": 130},
        {"fieldname": "cohort", "label": _("Cohort"), "fieldtype": "Link", "options": "Cohort", "width": 110},
        {"fieldname": "campus", "label": _("Campus"), "fieldtype": "Link", "options": "Campus", "width": 110},
        {"fieldname": "days_present", "label": _("Days present"), "fieldtype": "Int", "width": 110},
        {"fieldname": "days_absent", "label": _("Days absent"), "fieldtype": "Int", "width": 110},
        {"fieldname": "attendance_pct", "label": _("Attendance %"), "fieldtype": "Percent", "width": 110},
        {"fieldname": "total_active_hours", "label": _("Active hours"), "fieldtype": "Float", "precision": 1, "width": 110},
        {"fieldname": "avg_minutes_per_day", "label": _("Avg min/present day"), "fieldtype": "Int", "width": 140},
        {"fieldname": "last_seen", "label": _("Last seen"), "fieldtype": "Datetime", "width": 150},
    ]

    chart = {
        "data": {
            "labels": [r["student_name"] or r["student"] for r in rows],
            "datasets": [{"name": _("Attendance %"),
                          "values": [r["attendance_pct"] for r in rows]}],
        },
        "type": "bar",
        "colors": ["#6c5ce7"],
    } if rows else None

    pcts = [r["attendance_pct"] for r in rows]
    report_summary = [
        {"label": _("Students"), "value": len(rows), "datatype": "Int"},
        {"label": _("Class attendance %"),
         "value": (str(round(sum(pcts) / len(pcts))) + "%") if pcts else "—", "datatype": "Data"},
        {"label": _("Below 50%"), "value": sum(1 for p in pcts if p < 50), "datatype": "Int",
         "indicator": "Red" if any(p < 50 for p in pcts) else "Green"},
    ]

    return columns, rows, None, chart, report_summary
