// Copyright (c) 2026, FOSS United and contributors
// For license information, please see license.txt

frappe.query_reports["Daily Attendance"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -6),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "campus",
			label: __("Campus"),
			fieldtype: "Link",
			options: "Campus",
		},
		{
			fieldname: "cohort",
			label: __("Cohort"),
			fieldtype: "Link",
			options: "Cohort",
		},
		{
			fieldname: "student",
			label: __("Student"),
			fieldtype: "Link",
			options: "Student",
		},
		{
			fieldname: "hide_absent",
			label: __("Hide absent rows"),
			fieldtype: "Check",
			default: 0,
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data) {
			const color = data.status === "Present" ? "green" : "red";
			value = `<span class="indicator-pill ${color}">${data.status}</span>`;
		}
		return value;
	},
};
