// Copyright (c) 2026, FOSS United and contributors
// For license information, please see license.txt

// A facilitator's route to recording parental consent for a family the WhatsApp code cannot
// reach. WhatsApp needs a smartphone and mobile data, which the poorest households in the
// programme are the least likely to have — so without this button the consent gate quietly
// becomes a wealth filter, and the girls who most need the programme are the ones who cannot
// enrol. The backend method has always existed (hikmat.api.record_guardian_consent); it was
// reachable only by hand-crafting an API call, which is not a thing a facilitator will ever do,
// so in practice the fallback did not exist. This is the affordance.
//
// It is deliberately NOT presented as an easier alternative to the code: the dialog asks the
// facilitator to confirm they actually spoke to the guardian, and the record it writes is
// stamped channel="Facilitator" with their note, so an auditor can always tell an attested
// consent from a device-proven one. They are not the same evidence and must not look alike.
frappe.ui.form.on("Student", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.guardian_verified) {
			frm.dashboard.add_indicator(
				__("Guardian consent on file{0}", [
					frm.doc.guardian_mobile_last4 ? __(" (number ending {0})", [frm.doc.guardian_mobile_last4]) : "",
				]),
				"green"
			);
		} else {
			frm.dashboard.add_indicator(__("No guardian consent recorded"), "orange");
		}

		frm.add_custom_button(
			frm.doc.guardian_verified ? __("Re-record guardian consent") : __("Record guardian consent"),
			() => record_guardian_consent(frm),
			__("Guardian")
		);
	},
});

function record_guardian_consent(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Record guardian consent"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted">${__(
					"Use this only when you have actually spoken to this learner's parent or guardian and they agreed — in person, on a phone call, or on a signed form. For a guardian with WhatsApp, let them enter the code in the app instead: that proves the number, which an attestation cannot."
				)}</p>`,
			},
			{
				fieldname: "mobile",
				fieldtype: "Data",
				label: __("Guardian's mobile number (optional)"),
				description: __(
					"10 digits, Indian mobile. Only a keyed hash and the last 4 digits are stored — never the number itself. Leave blank if the family has no phone; consent is still recorded."
				),
			},
			{
				fieldname: "note",
				fieldtype: "Small Text",
				label: __("How was consent given?"),
				reqd: 1,
				description: __("e.g. 'Met her mother at the centre on 20 Aug, explained and she agreed.'"),
			},
			{
				fieldname: "confirm",
				fieldtype: "Check",
				label: __("I spoke to this learner's parent or guardian and they agreed."),
				reqd: 1,
			},
		],
		primary_action_label: __("Record consent"),
		primary_action(values) {
			if (!values.confirm) {
				frappe.msgprint(__("Please confirm you spoke to the parent or guardian."));
				return;
			}
			// The digits are normalised server-side too (_norm_mobile); this only spares the
			// facilitator a round trip for the commonest slip, a number typed with spaces.
			const mobile = (values.mobile || "").replace(/\D/g, "").slice(-10);
			if (values.mobile && mobile.length !== 10) {
				frappe.msgprint(__("That does not look like a 10-digit mobile number."));
				return;
			}
			d.get_primary_btn().prop("disabled", true);
			frappe.call({
				method: "hikmat.api.record_guardian_consent",
				args: { student: frm.doc.name, mobile: mobile || null, note: values.note },
				callback(r) {
					const res = r && r.message;
					if (res && res.ok) {
						d.hide();
						frappe.show_alert({ message: __("Consent recorded."), indicator: "green" });
						frm.reload_doc();
						return;
					}
					d.get_primary_btn().prop("disabled", false);
					frappe.msgprint(
						res && res.error === "bad_mobile"
							? __("That does not look like a 10-digit Indian mobile number.")
							: __("Could not record consent. Please try again.")
					);
				},
				error() {
					d.get_primary_btn().prop("disabled", false);
				},
			});
		},
	});
	d.show();
}
