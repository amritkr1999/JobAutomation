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

# Standardized Recruiter Pipeline Stages
VALID_STAGES = [
    "DISCOVERED",
    "OUTREACH_SENT",
    "REPLIED",
    "SCREENING_REQUESTED",
    "LINK_RECEIVED",
    "APPLIED_VIA_LINK",
    "INTERVIEW_SCHEDULED",
    "TECH_ASSESSMENT",
    "OFFER",
    "REJECTED",
    "GHOSTED"
]


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
    """Seed CRM with historical outreach and recruiter replies from sent_emails.json."""
    if not os.path.exists(SENT_EMAILS_FILE):
        return crm_data

    try:
        with open(SENT_EMAILS_FILE, 'r', encoding='utf-8') as f:
            sent_records = json.load(f)
    except Exception:
        return crm_data

    modified = False
    threads = crm_data.setdefault("threads", {})

    for norm_email, rec in sent_records.items():
        if norm_email not in threads:
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
                "raw_email": rec.get("raw_email", norm_email),
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


def record_outreach(email: str, company: str, role: str, recruiter_name: str = "",
                    source_channel: str = "COLD_EMAIL", metadata: dict = None) -> dict:
    """Record a newly dispatched outreach in CRM."""
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
    stage_arg = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else None
    print_crm_table(filter_stage=stage_arg)
