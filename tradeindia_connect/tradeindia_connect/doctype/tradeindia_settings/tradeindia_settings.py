# Copyright (c) 2025, NDV and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

TRADEINDIA_API_FIELDS = {
    "message": "",
    "sender_name": "first_name",
    "sender_mobile": "mobile_no",
    "sender_city": "",
    "sender_state": "",
    "sender_country": "",
    "sender_co": "organization",
    "sender_uid": "tradeindia_sender_uid",
    "sender_email": "email",
}

class TradeIndiaSettings(Document):
    def validate(self):
        """Keep target_field once mapped, prefill only first time"""
        # store old mappings
        existing_map = {d.response_field: d.target_field for d in self.get("table_xjfs")}

        # clear old rows
        self.set("table_xjfs", [])

        # rebuild response fields in order
        for field, default_target in TRADEINDIA_API_FIELDS.items():
            self.append("table_xjfs", {
                "response_field": field,
                "target_field": existing_map.get(field) or default_target
            })

