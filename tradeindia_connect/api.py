import frappe, requests, json
from frappe.utils import strip_html, getdate, add_days

@frappe.whitelist()
def fetch_tradeindia_inquiries(from_date=None, to_date=None):
    try:
        settings = frappe.get_single("TradeIndia Settings")
        api_key = settings.get_password("key")

        if not api_key:
            frappe.throw("API Key is not set in TradeIndia Settings. Please configure your API key.")

        # Check if lead_doctype is configured
        lead_doctype = settings.get("lead_doctype")
        if not lead_doctype:
            frappe.throw("Lead Doctype is not set in TradeIndia Settings. Please configure the Lead Doctype first.")

        # If no dates passed, take from settings
        if not from_date:
            from_date = settings.from_date
        if not to_date:
            to_date = settings.to_date

        from_date = getdate(from_date)
        to_date = getdate(to_date)

        all_inquiries = []
        leads_created = 0
        duplicates_skipped = 0
        failed_leads = 0
        api_urls = []

        cur_date = from_date
        while cur_date <= to_date:
            # This fetches inquiries for a specific day
            api_url = (
                "https://www.tradeindia.com/utils/my_inquiry.html"
                f"?userid={settings.user_id}&profile_id={settings.profile_id}"
                f"&key={api_key}&from_date={cur_date}&to_date={cur_date}"
            )
            api_urls.append(api_url)

            try:
                response = requests.get(api_url, headers={'Accept': 'application/json'}, timeout=30)
                response.raise_for_status()
            except requests.RequestException as e:
                error_title = f"TradeIndia API Error {cur_date}"
                error_message = f"Request Failed: {str(e)}\n\nAPI URL:\n{api_url}\n\nDate: {cur_date}\nUser ID: {settings.user_id}\nProfile ID: {settings.profile_id}"
                frappe.log_error(error_message, error_title)
                cur_date = add_days(cur_date, 1)
                continue

            if not response.text.strip():
                cur_date = add_days(cur_date, 1)
                continue

            try:
                data = response.json()
            except Exception:
                try:
                    data = json.loads(response.text.strip())
                except Exception as json_error:
                    parse_title = f"TradeIndia JSON Error {cur_date}"
                    parse_message = f"Invalid JSON Response: {str(json_error)}\n\nAPI URL:\n{api_url}\n\nRaw Response:\n{response.text[:1000]}{'...' if len(response.text) > 1000 else ''}"
                    frappe.log_error(parse_message, parse_title)
                    cur_date = add_days(cur_date, 1)
                    continue

            inquiries = []
            if isinstance(data, list):
                inquiries = data
            elif isinstance(data, dict):
                for key in ['inquiries', 'data', 'results', 'items', 'response']:
                    if isinstance(data.get(key), list):
                        inquiries = data[key]
                        break
                if not inquiries and any(k in data for k in ['sender_email', 'message']):
                    inquiries = [data]

            for inquiry in inquiries:
                if isinstance(inquiry, dict):
                    result = create_lead_from_inquiry(inquiry, settings, lead_doctype)
                    if result == "created":
                        leads_created += 1
                        all_inquiries.append(inquiry)
                    elif result == "duplicate":
                        duplicates_skipped += 1
                    elif result == "failed":
                        failed_leads += 1

            cur_date = add_days(cur_date, 1)

        # Show minimal popup with results
        result_message = f"""
        <div style="padding: 20px; font-family: inherit; line-height: 1.4;">
            
            <div style="margin-bottom: 24px;">
                <div style="font-weight: 600; margin-bottom: 4px;">✅ New Leads Created</div>
                <div style="font-size: 32px; font-weight: 700;">{leads_created}</div>
            </div>
            
            <div style="margin-bottom: 24px;">
                <div style="font-weight: 600; margin-bottom: 4px;">⚠️ Duplicates Skipped</div>
                <div style="font-size: 32px; font-weight: 700;">{duplicates_skipped}</div>
            </div>
            
            {f'''<div style="margin-bottom: 24px;">
                <div style="font-weight: 600; margin-bottom: 4px;">❌ Failed to Create</div>
                <div style="font-size: 32px; font-weight: 700;">{failed_leads}</div>
            </div>''' if failed_leads > 0 else ''}            
            
            <div style="font-size: 14px;">
                <div style="margin-bottom: 2px;"><strong>Total Leads :</strong> {len(all_inquiries) + duplicates_skipped + failed_leads}</div>
                <div style="margin-bottom: 2px;"><strong>Date Range :</strong> {from_date} to {to_date}</div>
                <div><strong>Doctype Used :</strong> {lead_doctype}</div>
            </div>
            
        </div>
        """
        
        frappe.msgprint(
            result_message,
            title="Fetch Complete",
            indicator="green"
        )

        return {
            "status": "success",
            "created": leads_created,
            "duplicates_skipped": duplicates_skipped,
            "failed_leads": failed_leads,
            "total_processed": len(all_inquiries) + duplicates_skipped + failed_leads,
            "urls_logged": len(api_urls),
            "doctype_used": lead_doctype
        }
        
    except Exception as e:
        frappe.throw(f"Error during import: {str(e)}")


def create_lead_from_inquiry(inquiry, settings, lead_doctype):
    lead_data = {}
    
    # Map fields from settings
    for mapping in settings.get("table_xjfs", []):
        resp, target = mapping.get("response_field"), mapping.get("target_field")
        if resp and target and inquiry.get(resp):
            val = str(inquiry.get(resp))
            if target == "mobile_no":
                val = val.replace("+91", "").replace("+", "").replace("-", "").replace(" ", "")
            lead_data[target] = val

    lead_data["source"] = settings.get("default_lead_source")
    lead_data["lead_owner"] = settings.get("default_lead_owner") or frappe.session.user

    # Add important fields only if present
    for f, v in {
        "company_name": inquiry.get("sender_co"),
        "city": inquiry.get("sender_city"),
        "state": inquiry.get("sender_state"),
        "country": inquiry.get("sender_country"),
        "email_id": inquiry.get("sender_email"),
    }.items():
        if v and f not in lead_data:
            lead_data[f] = v

    # Ensure first_name is always present (FIX for MandatoryError)
    if not lead_data.get("first_name"):
        # Try to extract from sender name if available
        sender_name = inquiry.get("sender_name") or inquiry.get("receiver_name", "")
        if sender_name:
            # Remove titles like "Mr", "Mrs", etc.
            name_parts = sender_name.replace("Mr ", "").replace("Mrs ", "").replace("Ms ", "").replace("Dr ", "").strip().split()
            lead_data["first_name"] = name_parts[0] if name_parts else "Client"
            if len(name_parts) > 1:
                lead_data["last_name"] = " ".join(name_parts[1:])
        else:
            # Fallback: use a generic name
            lead_data["first_name"] = "Client"
            
        # If we have company name but no personal name, use company-based naming
        if lead_data.get("company_name") and lead_data["first_name"] == "Client":
            lead_data["first_name"] = "Contact"
            lead_data["last_name"] = f"from {lead_data['company_name']}"

    # Updated validation - ensure first_name exists
    if not lead_data.get("first_name"):
        lead_error_title = "Lead Creation Failed"
        lead_error_message = f"Cannot create lead: first_name is mandatory\n\nInquiry Data:\n{json.dumps(inquiry, indent=2)}"
        frappe.log_error(lead_error_message, lead_error_title)
        return "failed"
        
    # Check for duplicates - Return "duplicate" if found (using configurable doctype)
    if lead_data.get("email_id") and frappe.db.exists(lead_doctype, {"email_id": lead_data["email_id"]}):
        return "duplicate"
    
    if lead_data.get("mobile_no") and frappe.db.exists(lead_doctype, {"mobile_no": lead_data["mobile_no"]}):
        return "duplicate"

    try:
        # Create the lead document using configurable doctype
        lead_doc = frappe.get_doc({"doctype": lead_doctype, **lead_data})
        lead_doc.insert(ignore_permissions=True)

        # Clean and format the message content
        raw_message = inquiry.get('message', '')
        
        # Remove HTML tags and clean up formatting
        clean_message = strip_html(raw_message)
        clean_message = clean_message.replace('\r\n', '\n').replace('\r', '\n')
        clean_message = clean_message.replace('-----------------------------------------------------------', '')
        clean_message = clean_message.replace('\nContact buyer with your best offer.\n', '')
        
        # Extract main inquiry message (before "Sender Details")
        if "Sender Details" in clean_message:
            inquiry_text = clean_message.split("Sender Details")[0].strip()
        else:
            inquiry_text = clean_message.strip()

        # Build clean HTML-formatted comment (NOT markdown)
        comment_text = f"""
<div>
<p><strong>Inquiry Details:</strong></p>

<p><strong>Subject:</strong> {inquiry.get('subject') or 'Not specified'}<br>
<strong>Product:</strong> {inquiry.get('product_name') or 'Not specified'}<br>
<strong>Quantity:</strong> {inquiry.get('quantity') or 'Not specified'}<br>
<strong>Location:</strong> {inquiry.get('sender_city') or ''}, {inquiry.get('sender_state') or ''}, {inquiry.get('sender_country') or ''}</p>
<p><strong>Requirements:</strong> {inquiry_text.replace(chr(10), '<br>')}</p>
</div>
        """.strip()

        # Create clean comment (using configurable doctype)
        frappe.get_doc({
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": lead_doctype,
            "reference_name": lead_doc.name,
            "content": comment_text
        }).insert(ignore_permissions=True)

        return "created"
        
    except Exception as e:
        creation_error_title = "Lead Creation Exception"
        creation_error_message = f"Failed to create lead: {str(e)}\n\nLead Data:\n{json.dumps(lead_data, indent=2)}\n\nInquiry Data:\n{json.dumps(inquiry, indent=2)}"
        frappe.log_error(creation_error_message, creation_error_title)
        return "failed"