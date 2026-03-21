import time
import random
import frappe
import requests
import json
from frappe.utils import strip_html, getdate, add_days, add_months, get_last_day


@frappe.whitelist()
def fetch_tradeindia_inquiries(from_date=None, to_date=None):
	try:
		settings = frappe.get_single("TradeIndia Settings")
		api_key = settings.get_password("key")

		if not api_key:
			frappe.throw("API Key is not set in TradeIndia Settings. Please configure your API key.")

		lead_doctype = settings.get("lead_doctype")
		if not lead_doctype:
			frappe.throw("Lead Doctype is not set in TradeIndia Settings. Please configure the Lead Doctype first.")

		if not from_date:
			from_date = settings.from_date
		if not to_date:
			to_date = settings.to_date

		from_date = getdate(from_date)
		to_date = getdate(to_date)

		result = _run_fetch(from_date, to_date, settings, lead_doctype)

	except Exception as e:
		frappe.throw(f"Error during import: {str(e)}")

	result_message = f"""
	<div style="padding: 20px; font-family: inherit; line-height: 1.4;">
		<div style="margin-bottom: 24px;">
			<div style="font-weight: 600; margin-bottom: 4px;">✅ New Leads Created</div>
			<div style="font-size: 32px; font-weight: 700;">{result['leads_created']}</div>
		</div>
		<div style="margin-bottom: 24px;">
			<div style="font-weight: 600; margin-bottom: 4px;">⚠️ Duplicates Skipped</div>
			<div style="font-size: 32px; font-weight: 700;">{result['duplicates_skipped']}</div>
		</div>
		{f'''<div style="margin-bottom: 24px;">
			<div style="font-weight: 600; margin-bottom: 4px;">❌ Failed to Create</div>
			<div style="font-size: 32px; font-weight: 700;">{result['failed_leads']}</div>
		</div>''' if result['failed_leads'] > 0 else ""}
		<div style="font-size: 14px;">
			<div style="margin-bottom: 2px;"><strong>Total Processed:</strong> {result['total_processed']}</div>
			<div style="margin-bottom: 2px;"><strong>Date Range:</strong> {from_date} to {to_date}</div>
			<div style="margin-bottom: 2px;"><strong>Chunks Fetched:</strong> {result['chunks_fetched']}</div>
			<div><strong>Doctype Used:</strong> {lead_doctype}</div>
		</div>
	</div>
	"""

	frappe.msgprint(result_message, title="Fetch Complete", indicator="green")

	return {
		"status": "success",
		**result,
		"doctype_used": lead_doctype,
	}


def fetch_tradeindia_inquiries_scheduled():
	"""Called by scheduler every 2 hours — no msgprint."""
	try:
		settings = frappe.get_single("TradeIndia Settings")
		api_key = settings.get_password("key")
		lead_doctype = settings.get("lead_doctype")

		if not api_key or not lead_doctype:
			frappe.log_error("TradeIndia Settings incomplete (missing key or lead_doctype)", "TradeIndia Scheduler")
			return

		# Fetch today only — scheduler runs every 2hr so today is sufficient
		today = getdate(frappe.utils.today())
		result = _run_fetch(today, today, settings, lead_doctype)

		frappe.logger().info(
			f"TradeIndia Scheduler: created={result['leads_created']} "
			f"dupes={result['duplicates_skipped']} failed={result['failed_leads']}"
		)

	except Exception as e:
		frappe.log_error(f"Scheduler error: {str(e)}", "TradeIndia Scheduler")


def _run_fetch(from_date, to_date, settings, lead_doctype):
	"""Core fetch loop — shared by whitelisted endpoint and scheduler."""
	api_key = settings.get_password("key")
	all_inquiries = []
	leads_created = 0
	duplicates_skipped = 0
	failed_leads = 0

	chunks = _get_monthly_chunks(from_date, to_date)

	for chunk_start, chunk_end in chunks:
		cur_date = chunk_start
		while cur_date <= chunk_end:
			api_url = (
				"https://www.tradeindia.com/utils/my_inquiry.html"
				f"?userid={settings.user_id}&profile_id={settings.profile_id}"
				f"&key={api_key}&from_date={cur_date}&to_date={cur_date}"
			)

			try:
				response = requests.get(api_url, headers={"Accept": "application/json"}, timeout=30)
				response.raise_for_status()
			except requests.RequestException as e:
				frappe.log_error(
					f"Request Failed: {str(e)}\n\nAPI URL:\n{api_url}\n\nDate: {cur_date}",
					f"TradeIndia API Error {cur_date}"
				)
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
					frappe.log_error(
						f"Invalid JSON: {str(json_error)}\n\nURL:\n{api_url}\n\nResponse:\n{response.text[:1000]}",
						f"TradeIndia JSON Error {cur_date}"
					)
					cur_date = add_days(cur_date, 1)
					continue

			inquiries = []
			if isinstance(data, list):
				inquiries = data
			elif isinstance(data, dict):
				for key in ["inquiries", "data", "results", "items", "response"]:
					if isinstance(data.get(key), list):
						inquiries = data[key]
						break
				if not inquiries and any(k in data for k in ["sender_email", "message"]):
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

	return {
		"leads_created": leads_created,
		"duplicates_skipped": duplicates_skipped,
		"failed_leads": failed_leads,
		"total_processed": len(all_inquiries) + duplicates_skipped + failed_leads,
		"chunks_fetched": len(chunks),
	}


def _get_monthly_chunks(from_date, to_date):
	chunks = []
	chunk_start = from_date
	while chunk_start <= to_date:
		chunk_end = min(get_last_day(chunk_start), to_date)
		chunks.append((chunk_start, chunk_end))
		chunk_start = add_days(chunk_end, 1)
	return chunks


def _insert_with_retry(doc, max_retries=3):
	for attempt in range(max_retries):
		try:
			doc.insert(ignore_permissions=True)
			return
		except Exception as e:
			if "1213" in str(e) and attempt < max_retries - 1:
				frappe.db.rollback()
				time.sleep(random.uniform(0.3, 0.9))
				continue
			raise


def create_lead_from_inquiry(inquiry, settings, lead_doctype):
	lead_data = {}

	for mapping in settings.get("table_xjfs", []):
		resp, target = mapping.get("response_field"), mapping.get("target_field")
		if resp and target and inquiry.get(resp):
			val = str(inquiry.get(resp))
			if target == "mobile_no":
				val = val.replace("+91", "").replace("+", "").replace("-", "").replace(" ", "")
			lead_data[target] = val

	lead_data["source"] = settings.get("default_lead_source")
	lead_data["lead_owner"] = settings.get("default_lead_owner") or frappe.session.user

	for f, v in {
		"company_name": inquiry.get("sender_co"),
		"email": inquiry.get("sender_email"),
	}.items():
		if v and f not in lead_data:
			lead_data[f] = v

	if not lead_data.get("first_name"):
		sender_name = inquiry.get("sender_name") or inquiry.get("receiver_name", "")
		if sender_name:
			name_parts = (
				sender_name
				.replace("Mr ", "").replace("Mrs ", "").replace("Ms ", "").replace("Dr ", "")
				.strip().split()
			)
			lead_data["first_name"] = name_parts[0] if name_parts else "Client"
			if len(name_parts) > 1:
				lead_data["last_name"] = " ".join(name_parts[1:])
		else:
			lead_data["first_name"] = "Client"

		if lead_data.get("company_name") and lead_data["first_name"] == "Client":
			lead_data["first_name"] = "Contact"
			lead_data["last_name"] = f"from {lead_data['company_name']}"

	if not lead_data.get("first_name"):
		frappe.log_error(
			f"Cannot create lead: first_name is mandatory\n\nInquiry:\n{json.dumps(inquiry, indent=2)}",
			"Lead Creation Failed"
		)
		return "failed"

	if lead_data.get("email") and frappe.db.exists(lead_doctype, {"email": lead_data["email"]}):
		return "duplicate"

	if lead_data.get("mobile_no") and frappe.db.exists(lead_doctype, {"mobile_no": lead_data["mobile_no"]}):
		return "duplicate"

	try:
		lead_doc = frappe.get_doc({"doctype": lead_doctype, **lead_data})
		lead_doc.flags.ignore_feed = True
		lead_doc.flags.ignore_permissions = True
		_insert_with_retry(lead_doc)

		raw_message = inquiry.get("message", "")
		clean_message = strip_html(raw_message)
		clean_message = clean_message.replace("\r\n", "\n").replace("\r", "\n")
		clean_message = clean_message.replace("-----------------------------------------------------------", "")
		clean_message = clean_message.replace("\nContact buyer with your best offer.\n", "")

		if "Sender Details" in clean_message:
			inquiry_text = clean_message.split("Sender Details")[0].strip()
		else:
			inquiry_text = clean_message.strip()

		comment_text = f"""
		<div>
			<p><strong>Inquiry Details:</strong></p>
			<p>
				<strong>Subject:</strong> {inquiry.get('subject') or 'Not specified'}<br>
				<strong>Product:</strong> {inquiry.get('product_name') or 'Not specified'}<br>
				<strong>Quantity:</strong> {inquiry.get('quantity') or 'Not specified'}<br>
				<strong>Location:</strong> {inquiry.get('sender_city') or ''}, {inquiry.get('sender_state') or ''}, {inquiry.get('sender_country') or ''}
			</p>
			<p><strong>Requirements:</strong> {inquiry_text.replace(chr(10), '<br>')}</p>
		</div>
		""".strip()

		frappe.get_doc({
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": lead_doctype,
			"reference_name": lead_doc.name,
			"content": comment_text,
		}).insert(ignore_permissions=True)

		return "created"

	except Exception as e:
		frappe.log_error(
			f"Failed to create lead: {str(e)}\n\nLead Data:\n{json.dumps(lead_data, indent=2)}\n\nInquiry:\n{json.dumps(inquiry, indent=2)}",
			"Lead Creation Exception"
		)
		return "failed"