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

		# Fetch yesterday + today to avoid missing leads around midnight
		today = getdate(frappe.utils.today())
		yesterday = add_days(today, -1)
		result = _run_fetch(yesterday, today, settings, lead_doctype)

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


def create_lead_from_inquiry(inquiry, settings, lead_doctype, source_override=None):
	lead_data = {}

	for mapping in settings.get("table_xjfs", []):
		resp, target = mapping.get("response_field"), mapping.get("target_field")
		if resp and target and inquiry.get(resp):
			val = str(inquiry.get(resp))
			if target == "mobile_no":
				val = val.replace("+91", "").replace("+", "").replace("-", "").replace(" ", "")
			lead_data[target] = val
			
	lead_data["source"] = source_override or settings.get("default_inquiry_source")
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

	rfi_id = str(inquiry.get("rfi_id") or "").strip()
	if rfi_id:
		lead_data["tradeindia_lead_id"] = rfi_id
		if frappe.db.exists(lead_doctype, {"tradeindia_lead_id": rfi_id}):
			return "duplicate"

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
	

@frappe.whitelist()
def fetch_tradeindia_buyleads(from_date=None, to_date=None, responded=0):
	try:
		settings = frappe.get_single("TradeIndia Settings")
		api_key = settings.get_password("key")

		if not api_key:
			frappe.throw("API Key is not set in TradeIndia Settings.")

		lead_doctype = settings.get("lead_doctype")
		if not lead_doctype:
			frappe.throw("Lead Doctype is not set in TradeIndia Settings.")

		if not from_date:
			from_date = settings.from_date
		if not to_date:
			to_date = settings.to_date

		from_date = getdate(from_date)
		to_date = getdate(to_date)

		result = _run_buylead_fetch(from_date, to_date, settings, lead_doctype, responded=int(responded))

	except Exception as e:
		frappe.throw(f"Error during BuyLead import: {str(e)}")

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
			<div style="margin-bottom: 2px;"><strong>Pages Fetched:</strong> {result['pages_fetched']}</div>
			<div><strong>Doctype Used:</strong> {lead_doctype}</div>
		</div>
	</div>
	"""

	frappe.msgprint(result_message, title="BuyLead Fetch Complete", indicator="green")

	return {"status": "success", **result, "doctype_used": lead_doctype}


def fetch_tradeindia_buyleads_scheduled():
	"""Called by scheduler — fetches yesterday + today's buyLeads (both latest + responded)."""
	try:
		settings = frappe.get_single("TradeIndia Settings")
		api_key = settings.get_password("key")
		lead_doctype = settings.get("lead_doctype")

		if not api_key or not lead_doctype:
			frappe.log_error("TradeIndia Settings incomplete", "TradeIndia BuyLead Scheduler")
			return

		today = getdate(frappe.utils.today())
		yesterday = add_days(today, -1)

		for responded in (0, 1):
			result = _run_buylead_fetch(yesterday, today, settings, lead_doctype, responded=responded)
			frappe.logger().info(
				f"TradeIndia BuyLead Scheduler (responded={responded}): "
				f"created={result['leads_created']} dupes={result['duplicates_skipped']} failed={result['failed_leads']}"
			)

	except Exception as e:
		frappe.log_error(f"BuyLead Scheduler error: {str(e)}", "TradeIndia BuyLead Scheduler")


def _run_buylead_fetch(from_date, to_date, settings, lead_doctype, responded=0, limit=50):
	"""Day-by-day + paginated buylead fetch (API restricts to 24hr windows)."""
	api_key = settings.get_password("key")
	leads_created = 0
	duplicates_skipped = 0
	failed_leads = 0
	pages_fetched = 0
	total_processed = 0

	cur_date = from_date
	while cur_date <= to_date:
		page_no = 1

		while True:
			api_url = (
				"https://www.tradeindia.com/utils/my_buy_leads.html"
				f"?userid={settings.user_id}&profile_id={settings.profile_id}"
				f"&key={api_key}&from_date={cur_date}&to_date={cur_date}"
				f"&limit={limit}&page_no={page_no}"
			)
			if responded:
				api_url += "&responded_buy_leads=1"

			try:
				response = requests.get(api_url, headers={"Accept": "application/json"}, timeout=30)
				response.raise_for_status()
			except requests.RequestException as e:
				frappe.log_error(
					f"Request Failed: {str(e)}\n\nURL:\n{api_url}",
					f"TradeIndia BuyLead API Error {cur_date} page={page_no}"
				)
				break

			if not response.text.strip():
				break

			stripped = response.text.strip()

			# API returns literal "null" when no buy leads exist for this date — not an error
			if stripped.lower() == "null":
				break

			# Catch plain-text error messages (e.g. "greater than 24 hours not allowed")
			if stripped.startswith("<") or not (stripped.startswith("{") or stripped.startswith("[")):
				frappe.log_error(
					f"Non-JSON response:\n{stripped[:500]}\n\nURL:\n{api_url}",
					f"TradeIndia BuyLead Response Error {cur_date}"
				)
				break

			try:
				data = response.json()
			except Exception as json_error:
				frappe.log_error(
					f"Invalid JSON: {str(json_error)}\n\nResponse:\n{response.text[:1000]}\n\nURL:\n{api_url}",
					f"TradeIndia BuyLead JSON Error {cur_date} page={page_no}"
				)
				break

			buy_leads = []
			if isinstance(data, list):
				buy_leads = data
			elif isinstance(data, dict):
				for key in ["buy_leads", "buyLeads", "inquiries", "data", "results", "items", "response"]:
					if isinstance(data.get(key), list):
						buy_leads = data[key]
						break
				if not buy_leads and any(k in data for k in ["sender_email", "message", "buyer_email"]):
					buy_leads = [data]

			if not buy_leads:
				break

			pages_fetched += 1

			for lead in buy_leads:
				if not isinstance(lead, dict):
					continue
				result = create_lead_from_buylead(lead, settings, lead_doctype)
				if result == "created":
					leads_created += 1
				elif result == "duplicate":
					duplicates_skipped += 1
				elif result == "failed":
					failed_leads += 1
				total_processed += 1

			if len(buy_leads) < limit:
				break  # last page

			page_no += 1

		cur_date = add_days(cur_date, 1)

	return {
		"leads_created": leads_created,
		"duplicates_skipped": duplicates_skipped,
		"failed_leads": failed_leads,
		"total_processed": total_processed,
		"pages_fetched": pages_fetched,
	}

def create_lead_from_buylead(lead, settings, lead_doctype):
	contact = lead.get("contact_details") or {}

	# --- Name ---
	raw_name = contact.get("user_name") or ""
	name_parts = (
		raw_name
		.replace("Mr ", "").replace("Mrs ", "").replace("Ms ", "").replace("Dr ", "")
		.strip().split()
	)
	first_name = name_parts[0] if name_parts else None
	last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else None

	co_name = lead.get("co_name") or ""
	if not first_name:
		if co_name:
			first_name = "Contact"
			last_name = f"from {co_name}"
		else:
			first_name = "Client"

	# --- Email ---
	email = contact.get("contact_email") or lead.get("sender_email") or ""
	if email.strip().upper() in ("NA", "N/A", "NONE", "NULL", ""):
		email = ""

	# --- Mobile ---
	mobile = contact.get("contact_number") or ""
	mobile = mobile.replace("+91", "").replace("+", "").replace("-", "").replace(" ", "")
	if len(mobile) < 7:
		mobile = ""

	# --- Unique ID ---
	lead_id = str(lead.get("lead_id") or "").strip()

	# --- Duplicate check ---
	if lead_id and frappe.db.exists(lead_doctype, {"tradeindia_lead_id": lead_id}):
		return "duplicate"
	if email and frappe.db.exists(lead_doctype, {"email": email}):
		return "duplicate"
	if mobile and frappe.db.exists(lead_doctype, {"mobile_no": mobile}):
		return "duplicate"

	# --- Lead data ---
	lead_data = {
		"first_name": first_name,
		"source": settings.get("default_buylead_source") or "TradeIndia BuyLead",
		"lead_owner": settings.get("default_lead_owner") or frappe.session.user,
	}
	if lead_id:
		lead_data["tradeindia_lead_id"] = lead_id
	if last_name:
		lead_data["last_name"] = last_name
	if email:
		lead_data["email"] = email
	if mobile:
		lead_data["mobile_no"] = mobile
	if co_name:
		lead_data["organization"] = co_name

	# city/state from contact_details (more reliable than top-level)
	city  = contact.get("city")  or lead.get("city")  or ""
	state = contact.get("state") or lead.get("state") or ""

	# map any extra fields via table_xjfs that make sense for buyLeads
	for mapping in settings.get("table_xjfs", []):
		resp, target = mapping.get("response_field"), mapping.get("target_field")
		if resp and target and lead.get(resp) and target not in lead_data:
			lead_data[target] = str(lead.get(resp))

	if lead_data.get("email", "").strip().upper() in ("NA", "N/A", "NONE", "NULL", ""):
		lead_data.pop("email", None)

	try:
		lead_doc = frappe.get_doc({"doctype": lead_doctype, **lead_data})
		lead_doc.flags.ignore_feed = True
		lead_doc.flags.ignore_permissions = True
		_insert_with_retry(lead_doc)

		# --- Comment ---
		raw_desc = lead.get("description") or ""
		clean_desc = raw_desc.replace("\r\n", "\n").replace("\r", "\n")
		clean_desc = clean_desc.replace("Contact buyer with your best offer.", "").strip()

		comment_text = f"""
		<div>
			<p><strong>BuyLead Details:</strong></p>
			<p>
				<strong>Product:</strong> {lead.get('product_name') or 'Not specified'}<br>
				<strong>Lead ID:</strong> {lead.get('lead_id') or 'N/A'}<br>
				<strong>Posted On:</strong> {lead.get('posted_on') or 'N/A'}<br>
				<strong>Location:</strong> {city}, {state}, {lead.get('country') or ''}
			</p>
			{f"<p><strong>Requirements:</strong> {clean_desc.replace(chr(10), '<br>')}</p>" if clean_desc else ""}
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
			f"Failed to create buylead: {str(e)}\n\nLead Data:\n{json.dumps(lead_data, indent=2)}\n\nRaw:\n{json.dumps(lead, indent=2)}",
			"BuyLead Creation Exception"
		)
		return "failed"