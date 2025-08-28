// Copyright (c) 2025, NDV and contributors
// For license information, please see license.txt

// frappe.ui.form.on("TradeIndia Settings", {
// 	refresh(frm) {

// 	},
// });


frappe.ui.form.on("TradeIndia Settings", {
    refresh: function(frm) {
        frm.add_custom_button("Fetch Inquiries", function() {
            frappe.call({
                method: "tradeindia_connect.api.fetch_tradeindia_inquiries",
                freeze: true,
                freeze_message: "Fetching Inquiries...",
                callback: function(r) {
                    if (!r.exc) {
                        frappe.msgprint("Inquiries fetched and Leads created successfully!");
                    }
                }
            });
        });
    }
});
