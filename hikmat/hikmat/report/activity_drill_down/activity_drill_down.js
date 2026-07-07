// Copyright (c) 2026, FOSS United and contributors
// For license information, please see license.txt

frappe.query_reports["Activity Drill-down"] = {
	filters: [
		{
			fieldname: "cohort",
			label: __("Cohort"),
			fieldtype: "Link",
			options: "Cohort",
		},
		{
			fieldname: "track",
			label: __("Track"),
			fieldtype: "Link",
			options: "Track",
		},
		{
			fieldname: "lesson",
			label: __("Lesson"),
			fieldtype: "Data",
		},
		{
			fieldname: "activity",
			label: __("Activity"),
			fieldtype: "Select",
			options: "\nlearn\nlisten\nspell\nphrase\ntalk\nquiz\ncode\nfix\nemail\nread\nreply",
		},
		{
			fieldname: "student",
			label: __("Student"),
			fieldtype: "Link",
			options: "Student",
		},
	],
};
