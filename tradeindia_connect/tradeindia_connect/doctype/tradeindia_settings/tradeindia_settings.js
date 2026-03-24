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
            });
        }, ("Get Data"));

        frm.add_custom_button("Fetch BuyLeads", function() {
            frappe.call({
                method: "tradeindia_connect.api.fetch_tradeindia_buyleads",
                freeze: true,
                freeze_message: "Fetching BuyLeads...",
            });
        }, ("Get Data"));

        frm.add_custom_button("Fetch Responded BuyLeads", function() {
            frappe.call({
                method: "tradeindia_connect.api.fetch_tradeindia_buyleads",
                args: { responded: 1 },
                freeze: true,
                freeze_message: "Fetching Responded BuyLeads...",
            });
        }, ("Get Data"));
    }
});