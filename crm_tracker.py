#!/usr/bin/env python3
"""
Recruiter CRM & Interaction Lifecycle Tracker (crm_tracker.py)
Centralized repository for tracking recruiter interactions, communication stages,
timeline events, extracted application links, and follow-ups across all pipelines.
"""

import os
import re
import json
import logging
from datetime import datetime

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
CRM_FILE = os.path.join(LOGS_DIR, 'recruiter_crm.json')
SENT_EMAILS_FILE = os.path.join(LOGS_DIR, 'sent_emails.json')
SUPPRESSION_FILE = os.path.join(LOGS_DIR, 'suppression_list.json')
COLD_QUEUE_FILE = os.path.join(LOGS_DIR, 'cold_email_queue.json')
DRAFTS_FILE = os.path.join(LOGS_DIR, 'pending_reply_drafts.json')
TASKS_FILE = os.path.join(LOGS_DIR, 'tasks.json')

# Standardized Recruiter Pipeline Stages
VALID_STAGES = [
    "DISCOVERED",
    "OUTREACH_SENT",
    "REPLIED",
    "REFERRAL_REQUIRED",
    "SCREENING_REQUESTED",
    "LINK_RECEIVED",
    "APPLIED_VIA_LINK",
    "INTERVIEW_SCHEDULED",
    "TECH_ASSESSMENT",
    "OFFER",
    "REJECTED",
    "GHOSTED"
]

# Validation Regex & Noise Exclusions
EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,63}$')
CORRUPT_SUFFIX_REGEX = re.compile(
    r'\.(?:com|org|net|in|io|ai|co|sa|ae|uk)\.(?:please|pay|thanks|roles|modmed|contact|chargepoint|is|email)$',
    re.IGNORECASE
)

IGNORED_CRM_NOISE_REGEX = re.compile(
    r'(?i)(postmaster|mailer-daemon|no-?reply|donotreply|do-not-reply|notifications?|alerts?|updates?|newsletters?|'
    r'marketing|promotions?|digest|mailer@|jobmessenger@|indeedapply@|jobs\.shine\.com|'
    r'shine\.com|monsterindia\.com|indeed\.com|naukri\.com|glassdoor\.com|'
    r'invitations@linkedin\.com|jobalerts-noreply@linkedin\.com|alert@naukri\.com|'
    r'messages-noreply@linkedin\.com|engage@indeed\.com|substack\.com|'
    r'flyai\.airindia\.com|em\.linkedin\.com|hello\.stackoverflow\.email|talent500\.co|'
    r'amazon\.in|hdfcbank|cdslstatement|grammarly|ambitionbox|remotehunter|'
    r'opportunities@foundit\.in|myworkday\.com|talent\.icims\.com|ppfas\.com|'
    r'team@hi\.wellfound\.com|accommodationrequests@maersk\.com)'
)


def normalize_email(email_str: str) -> str:
    """Normalize email for deduplication and consistent indexing."""
    if not email_str:
        return ""
    em = email_str.strip().lower()
    match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', em)
    if match:
        em = match.group(0)
    if '@' in em:
        local, domain = em.split('@', 1)
        if domain in ('gmail.com', 'googlemail.com'):
            local = local.split('+')[0].replace('.', '')
            domain = 'gmail.com'
        return f"{local}@{domain}".lower()
    return em.lower()


def load_suppression_data() -> dict:
    """Load suppression data safely from disk."""
    if os.path.exists(SUPPRESSION_FILE):
        try:
            with open(SUPPRESSION_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logging.debug(f"[CRM] Suppression file read error: {e}")
    return {"suppressed_emails": {}, "suppressed_domains": {}}


def load_sent_records() -> dict:
    """Load sent emails record safely from disk."""
    if os.path.exists(SENT_EMAILS_FILE):
        try:
            with open(SENT_EMAILS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logging.debug(f"[CRM] Sent emails file read error: {e}")
    return {}


def is_valid_recruiter_email(email_str: str) -> bool:
    """Validate that email is syntactically sound and not a known noise / corrupt address."""
    if not email_str or not isinstance(email_str, str):
        return False
    em = email_str.strip().lower()
    if not EMAIL_REGEX.match(em):
        return False
    if '..' in em or em.endswith('.'):
        return False
    if CORRUPT_SUFFIX_REGEX.search(em):
        return False
    # Exception: allow legitimate company career/talent/people no-reply notices (e.g. careers-noreply@zomato.com)
    if re.search(r'(?i)(careers|talent|hiring|recruiting|people|jobs).*no-?reply', em):
        return True
    if IGNORED_CRM_NOISE_REGEX.search(em):
        return False
    return True


def is_suppressed_or_bounced(email_str: str, supp_data: dict = None, sent_records: dict = None) -> tuple:
    """Check if email or its domain is suppressed or has a BOUNCED status."""
    norm = normalize_email(email_str)
    raw = email_str.strip().lower()

    if supp_data is None:
        supp_data = load_suppression_data()
    if sent_records is None:
        sent_records = load_sent_records()

    # Generate dot-stripped variant for cross-domain bounce correlation
    local_part, domain = norm.split('@', 1) if '@' in norm else (norm, '')
    nodot = f"{local_part.replace('.', '')}@{domain}".lower() if domain else norm

    supp_emails = supp_data.get("suppressed_emails", {})
    for candidate in (norm, raw, nodot):
        if candidate in supp_emails:
            return True, f"SUPPRESSED_EMAIL ({supp_emails[candidate].get('reason', 'BLOCKED')})"

    supp_domains = supp_data.get("suppressed_domains", {})
    if domain and domain in supp_domains and domain not in ('gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com'):
        return True, f"SUPPRESSED_DOMAIN (@{domain})"

    for candidate in (norm, raw, nodot):
        if candidate in sent_records and isinstance(sent_records[candidate], dict):
            if sent_records[candidate].get("status") == "BOUNCED":
                return True, "BOUNCED_IN_SENT_RECORDS"

    return False, ""


def load_crm() -> dict:
    """Safely load CRM database with automatic bootstrap from sent_emails.json if empty."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    crm_data = {"version": "1.0", "last_updated": "", "threads": {}}

    if os.path.exists(CRM_FILE):
        try:
            with open(CRM_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if isinstance(loaded, dict) and "threads" in loaded:
                    crm_data = loaded
        except Exception as e:
            logging.error(f"[CRM] Error reading {CRM_FILE}: {e}")

    # Bootstrap from sent_emails.json if fresh or missing leads
    crm_data = _bootstrap_from_sent_emails(crm_data)
    return crm_data


def save_crm(crm_data: dict) -> bool:
    """Atomically save CRM database to disk."""
    crm_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    temp_path = CRM_FILE + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(crm_data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(temp_path, CRM_FILE)
        return True
    except Exception as e:
        logging.error(f"[CRM] Failed to save {CRM_FILE}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return False


def _bootstrap_from_sent_emails(crm_data: dict) -> dict:
    """Seed CRM with historical outreach and recruiter replies from sent_emails.json, excluding bounced/suppressed/invalid/noise leads."""
    if not os.path.exists(SENT_EMAILS_FILE):
        return crm_data

    sent_records = load_sent_records()
    if not sent_records:
        return crm_data

    supp_data = load_suppression_data()

    modified = False
    threads = crm_data.setdefault("threads", {})

    for norm_email, rec in sent_records.items():
        if norm_email in threads:
            continue

        raw_email = rec.get("raw_email", norm_email)

        # 1. Skip hard bounced emails in sent_records
        if rec.get("status") == "BOUNCED":
            continue

        # 2. Skip suppressed emails or domains
        is_supp, _ = is_suppressed_or_bounced(norm_email, supp_data, sent_records)
        if is_supp:
            continue
        is_supp_raw, _ = is_suppressed_or_bounced(raw_email, supp_data, sent_records)
        if is_supp_raw:
            continue

        # 3. Skip invalid, corrupted, or non-HR noise emails
        if not is_valid_recruiter_email(norm_email) or not is_valid_recruiter_email(raw_email):
            continue

        is_replied = rec.get("outcome") == "REPLIED"
        stage = "REPLIED" if is_replied else "OUTREACH_SENT"

        timeline = [
            {
                "timestamp": rec.get("sent_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "action": "OUTREACH_SENT",
                "summary": f"Sent cold application for {rec.get('job_title', 'Role')} at {rec.get('company', 'Company')}."
            }
        ]
        if is_replied:
            timeline.append({
                "timestamp": rec.get("replied_at", rec.get("sent_at", "")),
                "action": "REPLY_RECEIVED",
                "summary": "Recruiter replied to outreach email."
            })

        threads[norm_email] = {
            "recruiter_name": rec.get("recruiter_name", ""),
            "company": rec.get("company", "Company"),
            "role": rec.get("job_title", "Software Engineer"),
            "contact_email": norm_email,
            "raw_email": raw_email,
            "source_channel": "COLD_EMAIL",
            "stage": stage,
            "sentiment": "NEUTRAL" if not is_replied else "POSITIVE",
            "thread_message_ids": [],
            "created_at": rec.get("sent_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "last_interaction": rec.get("replied_at") or rec.get("sent_at", ""),
            "timeline": timeline,
            "extracted_links": [],
            "notes": []
        }
        modified = True

    if modified:
        save_crm(crm_data)

    return crm_data


def delete_contact(email: str, reason: str = "") -> bool:
    """
    Safely delete a recruiter contact from CRM threads.
    Matches normalized key, raw email, or contact email.
    """
    if not email:
        return False
    norm = normalize_email(email)
    raw = email.strip().lower()
    crm = load_crm()
    threads = crm.get("threads", {})

    deleted = False
    keys_to_remove = []
    for k, v in threads.items():
        k_lower = k.lower()
        c_email = v.get("contact_email", "").strip().lower()
        r_email = v.get("raw_email", "").strip().lower()
        if k_lower in (norm, raw) or c_email in (norm, raw) or r_email in (norm, raw):
            keys_to_remove.append(k)

    for k in set(keys_to_remove):
        del threads[k]
        deleted = True
        logging.info(f"[CRM] 🗑️ Deleted contact '{k}' from CRM. Reason: {reason or 'MANUAL_DELETION'}")

    if deleted:
        save_crm(crm)
        _clean_orphan_drafts_and_tasks({norm, raw})
    return deleted


def _clean_orphan_drafts_and_tasks(purged_emails: set):
    """Cancel or clean up pending drafts, tasks, and cold queues for deleted emails."""
    if not purged_emails:
        return

    purged_lower = {e.lower() for e in purged_emails if e}

    # 1. Clean pending_reply_drafts.json
    if os.path.exists(DRAFTS_FILE):
        try:
            with open(DRAFTS_FILE, 'r', encoding='utf-8') as f:
                drafts_obj = json.load(f)
            drafts_dict = drafts_obj.get("drafts", {})
            changed_drafts = False
            for did, dinfo in drafts_dict.items():
                r_em = dinfo.get("recruiter_email", "").strip().lower()
                norm_r = normalize_email(r_em)
                if r_em in purged_lower or norm_r in purged_lower:
                    if dinfo.get("status") not in ("CANCELLED", "DISMISSED"):
                        dinfo["status"] = "CANCELLED"
                        dinfo["cancelled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        dinfo["cancel_reason"] = "Recruiter email deleted from CRM / not working"
                        changed_drafts = True
            if changed_drafts:
                with open(DRAFTS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(drafts_obj, f, indent=2)
        except Exception as e:
            logging.debug(f"[CRM] Draft cleanup note: {e}")

    # 2. Clean tasks.json
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, 'r', encoding='utf-8') as f:
                tasks_obj = json.load(f)
            tasks_dict = tasks_obj.get("tasks", {})
            changed_tasks = False
            for tid, tinfo in tasks_dict.items():
                r_em = tinfo.get("recruiter_email", "").strip().lower()
                norm_r = normalize_email(r_em)
                if r_em in purged_lower or norm_r in purged_lower:
                    if tinfo.get("status") not in ("COMPLETED", "DISMISSED"):
                        tinfo["status"] = "DISMISSED"
                        tinfo["dismissed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        tinfo["notes"] = "Auto-dismissed: contact deleted as not working"
                        changed_tasks = True
            if changed_tasks:
                with open(TASKS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(tasks_obj, f, indent=2)
        except Exception as e:
            logging.debug(f"[CRM] Task cleanup note: {e}")

    # 3. Clean cold_email_queue.json
    if os.path.exists(COLD_QUEUE_FILE):
        try:
            with open(COLD_QUEUE_FILE, 'r', encoding='utf-8') as f:
                cqueue = json.load(f)
            if isinstance(cqueue, list):
                new_q = []
                for item in cqueue:
                    em = item.get("email", "").strip().lower()
                    if em not in purged_lower and normalize_email(em) not in purged_lower:
                        new_q.append(item)
                if len(new_q) != len(cqueue):
                    with open(COLD_QUEUE_FILE, 'w', encoding='utf-8') as f:
                        json.dump(new_q, f, indent=2)
        except Exception as e:
            logging.debug(f"[CRM] Cold queue cleanup note: {e}")


def purge_not_working_emails() -> dict:
    """
    Scan CRM database and permanently delete all:
      1. Hard bounced emails (from sent_emails and suppression_list)
      2. Globally suppressed emails and domains
      3. Corrupted OCR / invalid syntax email addresses
      4. Automated transactional / non-recruiter noise emails
    Also cleans up pending drafts and task queues for purged emails.
    """
    crm = load_crm()
    threads = crm.get("threads", {})
    supp_data = load_suppression_data()
    sent_records = load_sent_records()

    initial_count = len(threads)
    to_delete = {}

    for norm_email, data in list(threads.items()):
        raw_email = data.get("raw_email", norm_email)
        reasons = []

        # Check suppression or bounce
        is_supp, supp_reason = is_suppressed_or_bounced(norm_email, supp_data, sent_records)
        if not is_supp:
            is_supp, supp_reason = is_suppressed_or_bounced(raw_email, supp_data, sent_records)
        if is_supp:
            reasons.append(supp_reason)

        # Check syntax, corrupt TLD, and noise
        if not is_valid_recruiter_email(norm_email) or not is_valid_recruiter_email(raw_email):
            if CORRUPT_SUFFIX_REGEX.search(norm_email) or CORRUPT_SUFFIX_REGEX.search(raw_email):
                reasons.append("CORRUPTED_TLD_ARTIFACT")
            elif IGNORED_CRM_NOISE_REGEX.search(norm_email) or IGNORED_CRM_NOISE_REGEX.search(raw_email):
                reasons.append("AUTOMATED_NON_HR_NOISE")
            elif not EMAIL_REGEX.match(norm_email) or not EMAIL_REGEX.match(raw_email):
                reasons.append("INVALID_SYNTAX")
            elif ".." in norm_email or ".." in raw_email:
                reasons.append("DOUBLE_DOT")

        if reasons:
            to_delete[norm_email] = {
                "raw_email": raw_email,
                "company": data.get("company", "Unknown"),
                "role": data.get("role", "Unknown"),
                "reasons": reasons,
                "stage": data.get("stage", "UNKNOWN")
            }

    purged_count = 0
    all_purged_addrs = set()
    for norm_email, details in to_delete.items():
        if norm_email in threads:
            del threads[norm_email]
            purged_count += 1
            all_purged_addrs.add(norm_email)
            if details.get("raw_email"):
                all_purged_addrs.add(details["raw_email"].lower())
            logging.info(f"[CRM] 🗑️ Purged not-working contact: {norm_email} ({details['company']}) — {', '.join(details['reasons'])}")

    if purged_count > 0:
        save_crm(crm)

    # Clean up associated drafts, tasks, and queues
    _clean_orphan_drafts_and_tasks(all_purged_addrs)

    return {
        "initial_contacts": initial_count,
        "purged_count": purged_count,
        "remaining_contacts": len(threads),
        "purged": to_delete
    }


def record_outreach(email: str, company: str, role: str, recruiter_name: str = "",
                    source_channel: str = "COLD_EMAIL", metadata: dict = None) -> dict:
    """Record a newly dispatched outreach in CRM."""
    if not is_valid_recruiter_email(email):
        logging.warning(f"[CRM] Skipping outreach recording: invalid or noise address '{email}'.")
        return {}

    is_supp, reason = is_suppressed_or_bounced(email)
    if is_supp:
        logging.warning(f"[CRM] Skipping outreach recording: '{email}' is {reason}.")
        return {}

    norm = normalize_email(email)
    crm = load_crm()
    threads = crm.setdefault("threads", {})
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    entry = threads.get(norm, {
        "recruiter_name": recruiter_name,
        "company": company,
        "role": role,
        "contact_email": norm,
        "raw_email": email,
        "source_channel": source_channel,
        "stage": "OUTREACH_SENT",
        "sentiment": "NEUTRAL",
        "thread_message_ids": [],
        "created_at": now_str,
        "last_interaction": now_str,
        "timeline": [],
        "extracted_links": [],
        "notes": []
    })

    if recruiter_name and not entry.get("recruiter_name"):
        entry["recruiter_name"] = recruiter_name
    if company and entry.get("company") in ("Company", ""):
        entry["company"] = company
    if role and entry.get("role") in ("Software Engineer", "Role", ""):
        entry["role"] = role

    entry["stage"] = "OUTREACH_SENT"
    entry["last_interaction"] = now_str
    entry["timeline"].append({
        "timestamp": now_str,
        "action": "OUTREACH_SENT",
        "summary": f"Dispatched outreach to {email} ({company} - {role}).",
        "metadata": metadata or {}
    })

    threads[norm] = entry
    save_crm(crm)
    return entry


def record_incoming_message(email: str, subject: str, body_snippet: str, message_id: str = "",
                            recruiter_name: str = "", company: str = "", role: str = "",
                            intent: str = "GENERAL_INQUIRY", sentiment: str = "POSITIVE",
                            links: list = None) -> dict:
    """Record an incoming recruiter email, updating the CRM stage, links, and timeline."""
    if not is_valid_recruiter_email(email):
        logging.warning(f"[CRM] Skipping incoming recording: invalid or noise address '{email}'.")
        return {}

    is_supp, reason = is_suppressed_or_bounced(email)
    if is_supp:
        logging.warning(f"[CRM] Skipping incoming recording: '{email}' is {reason}.")
        return {}

    norm = normalize_email(email)
    crm = load_crm()
    threads = crm.setdefault("threads", {})
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    entry = threads.get(norm, {
        "recruiter_name": recruiter_name,
        "company": company or "Unknown Company",
        "role": role or "Software Engineer",
        "contact_email": norm,
        "raw_email": email,
        "source_channel": "INBOUND_EMAIL",
        "stage": "REPLIED",
        "sentiment": sentiment,
        "thread_message_ids": [],
        "created_at": now_str,
        "last_interaction": now_str,
        "timeline": [],
        "extracted_links": [],
        "notes": []
    })

    if recruiter_name and not entry.get("recruiter_name"):
        entry["recruiter_name"] = recruiter_name
    if company and entry.get("company") in ("Unknown Company", "Company", ""):
        entry["company"] = company
    if role and entry.get("role") in ("Software Engineer", "Role", ""):
        entry["role"] = role

    if message_id and message_id not in entry.get("thread_message_ids", []):
        entry.setdefault("thread_message_ids", []).append(message_id)

    # Determine stage progression
    if intent in ("SCREENING_REQUEST", "CALL_AVAILABILITY", "INTERVIEW_INVITE"):
        entry["stage"] = "SCREENING_REQUESTED"
    elif intent in ("LINK_PROVIDED", "APPLICATION_LINK_REQUIRED") or (links and len(links) > 0):
        entry["stage"] = "LINK_RECEIVED"
    elif intent in ("TECH_ASSESSMENT", "ASSESSMENT_TEST"):
        entry["stage"] = "TECH_ASSESSMENT"
    elif intent in ("REJECTION", "REJECT"):
        entry["stage"] = "REJECTED"
    elif intent == "REFERRAL_ONLY_POLICY":
        entry["stage"] = "REFERRAL_REQUIRED"
    else:
        entry["stage"] = "REPLIED"

    entry["sentiment"] = sentiment
    entry["last_interaction"] = now_str

    # Record extracted application links
    if links:
        existing_urls = {l.get("url") for l in entry.get("extracted_links", [])}
        for link_obj in links:
            u = link_obj.get("url") if isinstance(link_obj, dict) else str(link_obj)
            ats_type = link_obj.get("ats_type", "unknown") if isinstance(link_obj, dict) else "unknown"
            if u and u not in existing_urls:
                entry.setdefault("extracted_links", []).append({
                    "url": u,
                    "ats_type": ats_type,
                    "status": "PENDING",
                    "discovered_at": now_str,
                    "applied_at": "",
                    "screenshot": "",
                    "notes": ""
                })
                existing_urls.add(u)

    # Add timeline event
    entry.setdefault("timeline", []).append({
        "timestamp": now_str,
        "action": "INCOMING_EMAIL",
        "subject": subject,
        "intent": intent,
        "summary": body_snippet[:150] + ("..." if len(body_snippet) > 150 else ""),
        "links_count": len(links) if links else 0
    })

    threads[norm] = entry
    save_crm(crm)
    return entry


def record_link_application(email: str, url: str, ats_type: str, success: bool,
                            screenshot: str = "", notes: str = "") -> bool:
    """Record that an application link sent by a recruiter was executed."""
    norm = normalize_email(email)
    crm = load_crm()
    entry = crm.get("threads", {}).get(norm)
    if not entry:
        return False

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Update link status
    found_link = False
    for l in entry.get("extracted_links", []):
        if l.get("url") == url or url in l.get("url", ""):
            l["status"] = "COMPLETED" if success else "FAILED"
            l["ats_type"] = ats_type
            l["applied_at"] = now_str
            l["screenshot"] = screenshot
            l["notes"] = notes
            found_link = True
            break

    if not found_link:
        entry.setdefault("extracted_links", []).append({
            "url": url,
            "ats_type": ats_type,
            "status": "COMPLETED" if success else "FAILED",
            "discovered_at": now_str,
            "applied_at": now_str,
            "screenshot": screenshot,
            "notes": notes
        })

    if success:
        entry["stage"] = "APPLIED_VIA_LINK"

    entry["last_interaction"] = now_str
    entry.setdefault("timeline", []).append({
        "timestamp": now_str,
        "action": "LINK_APPLICATION_EXECUTED",
        "summary": f"{'Successfully completed' if success else 'Failed to complete'} {ats_type.upper()} application at {url}.",
        "screenshot": screenshot,
        "success": success
    })

    save_crm(crm)
    return True


def update_stage(email: str, new_stage: str, note: str = "") -> bool:
    """Update lifecycle stage manually or programmatically."""
    if new_stage not in VALID_STAGES:
        logging.warning(f"[CRM] Invalid stage '{new_stage}'. Must be one of {VALID_STAGES}")
        return False

    norm = normalize_email(email)
    crm = load_crm()
    entry = crm.get("threads", {}).get(norm)
    if not entry:
        return False

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old_stage = entry.get("stage", "UNKNOWN")
    entry["stage"] = new_stage
    entry["last_interaction"] = now_str
    entry.setdefault("timeline", []).append({
        "timestamp": now_str,
        "action": "STAGE_CHANGED",
        "summary": f"Stage transitioned from {old_stage} -> {new_stage}. Note: {note}"
    })

    save_crm(crm)
    return True


def get_summary() -> dict:
    """Return high-level CRM stage counts and active lead statistics."""
    crm = load_crm()
    threads = crm.get("threads", {})

    stage_counts = {stage: 0 for stage in VALID_STAGES}
    for t in threads.values():
        st = t.get("stage", "UNKNOWN")
        if st in stage_counts:
            stage_counts[st] += 1

    total = len(threads)
    active_positive = (
        stage_counts["REPLIED"] +
        stage_counts["SCREENING_REQUESTED"] +
        stage_counts["LINK_RECEIVED"] +
        stage_counts["APPLIED_VIA_LINK"] +
        stage_counts["INTERVIEW_SCHEDULED"] +
        stage_counts["TECH_ASSESSMENT"] +
        stage_counts["OFFER"]
    )

    return {
        "total_contacts": total,
        "active_engagements": active_positive,
        "stage_breakdown": stage_counts,
        "last_updated": crm.get("last_updated", "")
    }


def print_crm_table(limit: int = 30, filter_stage: str = None):
    """Format and print an interactive ASCII CRM dashboard in the console."""
    crm = load_crm()
    threads = list(crm.get("threads", {}).values())

    if filter_stage:
        threads = [t for t in threads if t.get("stage") == filter_stage.upper()]

    # Sort by last_interaction descending
    threads.sort(key=lambda x: x.get("last_interaction", ""), reverse=True)

    summary = get_summary()
    print("\n" + "=" * 90)
    print(f"📊 RECRUITER RELATIONSHIP CRM — CANDIDATE PIPELINE DASHBOARD")
    print(f"Total Contacts: {summary['total_contacts']} | Active Engagements: {summary['active_engagements']}")
    stage_summary_str = " | ".join([f"{k}: {v}" for k, v in summary['stage_breakdown'].items() if v > 0])
    print(f"Stages: {stage_summary_str}")
    print("=" * 90)

    header = f"{'COMPANY':<22} {'RECRUITER / EMAIL':<30} {'STAGE':<20} {'LAST TOUCH':<16}"
    print(header)
    print("-" * 90)

    stage_icons = {
        "OUTREACH_SENT": "📤 SENT",
        "REPLIED": "💬 REPLIED",
        "SCREENING_REQUESTED": "📞 SCREENING",
        "LINK_RECEIVED": "🔗 LINK RCVD",
        "APPLIED_VIA_LINK": "✅ APPLIED LINK",
        "INTERVIEW_SCHEDULED": "🗓️ INTERVIEW",
        "TECH_ASSESSMENT": "💻 ASSESSMENT",
        "OFFER": "🎉 OFFER",
        "REJECTED": "❌ REJECTED",
        "GHOSTED": "👻 GHOSTED",
        "DISCOVERED": "🔍 DISCOVERED"
    }

    display_threads = threads[:limit]
    for t in display_threads:
        co = (t.get("company") or "Unknown")[:20]
        contact = (t.get("recruiter_name") or t.get("raw_email") or t.get("contact_email") or "")[:28]
        st = stage_icons.get(t.get("stage"), t.get("stage", "UNKNOWN"))
        last_t = t.get("last_interaction", "")[:16]
        print(f"{co:<22} {contact:<30} {st:<20} {last_t:<16}")

    if len(threads) > limit:
        print(f"... and {len(threads) - limit} older contacts. (Use --limit to view all)")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if "--purge" in sys.argv:
        print("\n" + "=" * 70)
        print("🧹 PURGING NOT-WORKING / BOUNCED / NOISE RECRUITER CONTACTS")
        print("=" * 70)
        res = purge_not_working_emails()
        print(f"• Initial Contacts:   {res['initial_contacts']}")
        print(f"• Purged Contacts:    {res['purged_count']}")
        print(f"• Remaining Contacts: {res['remaining_contacts']}")
        print("=" * 70)
        print_crm_table(limit=30)
    elif "--delete" in sys.argv:
        idx = sys.argv.index("--delete")
        if idx + 1 < len(sys.argv):
            target_em = sys.argv[idx + 1]
            ok = delete_contact(target_em, reason="CLI_MANUAL_DELETE")
            print(f"Deleted contact {target_em}: {ok}")
        else:
            print("Usage: python3 crm_tracker.py --delete <email>")
    else:
        stage_arg = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else None
        print_crm_table(filter_stage=stage_arg)
