#!/usr/bin/env python3
"""
Inbound Recruiter Email Harvester & Intent Classifier (recruiter_harvester.py)
Scans Gmail INBOX for recruiter replies and new inbound hiring inquiries.
Extracts message context, intent, application links, and automatically triggers
CRM updates, task creation, link applications, and response drafting.
"""

import os
import re
import json
import logging
import imaplib
import email as email_lib
from email.header import decode_header
from datetime import datetime, timedelta
from urllib.parse import urlparse

import crm_tracker
import task_manager
import ats_bridge

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')

# Heuristics for Noise, Newsletters, Promo Ads and Automated Broadcast Exclusion
IGNORED_SENDERS_REGEX = re.compile(
    r'(?i)(postmaster|mailer-daemon|no-?reply|donotreply|do-not-reply|notifications?|alerts?|updates?|newsletters?|'
    r'marketing|promotions?|digest|mailer@|jobmessenger@|indeedapply@|jobs\.shine\.com|'
    r'shine\.com|monsterindia\.com|indeed\.com|naukri\.com|glassdoor\.com|'
    r'invitations@linkedin\.com|jobalerts-noreply@linkedin\.com|alert@naukri\.com|'
    r'messages-noreply@linkedin\.com|engage@indeed\.com|substack\.com|'
    r'flyai\.airindia\.com|em\.linkedin\.com|hello\.stackoverflow\.email|talent500\.co|'
    r'support@|billing@|info@|contact@|feedback@|events?@|webinars?@|'
    r'amazon\.in|hdfcbank|cdslstatement|grammarly|ambitionbox|remotehunter|'
    r'opportunities@foundit\.in|myworkday\.com|talent\.icims\.com|ppfas\.com|'
    r'team@hi\.wellfound\.com|accommodationrequests@maersk\.com)'
)

HIRING_KEYWORDS = [
    "interview", "screening", "shortlist", "application", "job opportunity",
    "recruiter", "talent acquisition", "opening", "assessment", "hackerrank",
    "codility", "technical discussion", "profile", "forms.gle", "docs.google.com/forms",
    "forms.office.com", "greenhouse.io", "myworkdayjobs.com", "notice period",
    "current ctc", "expected ctc", "resume", "cv", "hiring", "schedule a call",
    "referral", "employee referrals", "vouch for you", "internal referral"
]


def load_config() -> dict:
    """Load configuration safely."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"[Harvester] Config read error: {e}")
    return {}


def decode_mime_header(header_value: str) -> str:
    """Cleanly decode encoded RFC 2047 MIME headers."""
    if not header_value:
        return ""
    decoded_fragments = []
    for fragment, charset in decode_header(header_value):
        if isinstance(fragment, bytes):
            try:
                decoded_fragments.append(fragment.decode(charset or 'utf-8', errors='ignore'))
            except Exception:
                decoded_fragments.append(fragment.decode('latin1', errors='ignore'))
        else:
            decoded_fragments.append(str(fragment))
    return " ".join(decoded_fragments).strip()


def parse_sender_info(from_header: str) -> tuple:
    """Extract display name and normalized email address from From: header."""
    decoded = decode_mime_header(from_header)
    name = ""
    email_addr = ""

    match = re.search(r'(.*?)\s*<([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>', decoded)
    if match:
        name = match.group(1).strip().strip('"\'')
        email_addr = match.group(2).strip().lower()
    else:
        addr_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', decoded)
        if addr_match:
            email_addr = addr_match.group(0).lower()
            name = email_addr.split('@')[0].replace('.', ' ').title()

    return name, email_addr


def extract_body_and_links(msg_obj) -> tuple:
    """Extract clean plain text, html body, and embedded URLs from email message object."""
    plain_text = ""
    html_text = ""

    if msg_obj.is_multipart():
        for part in msg_obj.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in content_disposition:
                continue

            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or 'utf-8'
                decoded_text = payload.decode(charset, errors='ignore')

                if content_type == "text/plain":
                    plain_text += decoded_text + "\n"
                elif content_type == "text/html":
                    html_text += decoded_text + "\n"
            except Exception:
                continue
    else:
        try:
            payload = msg_obj.get_payload(decode=True)
            if payload:
                charset = msg_obj.get_content_charset() or 'utf-8'
                text = payload.decode(charset, errors='ignore')
                if msg_obj.get_content_type() == "text/html":
                    html_text = text
                else:
                    plain_text = text
        except Exception:
            pass

    full_text = plain_text if plain_text.strip() else re.sub(r'<[^>]+>', ' ', html_text)
    full_text = re.sub(r'\s+', ' ', full_text).strip()

    # Extract all candidate URLs
    raw_urls = re.findall(r'https?://[^\s\'"<>\)]+', plain_text + " " + html_text)
    cleaned_urls = []
    seen = set()

    # Known non-job URL domains to ignore
    ignore_domains = {
        'schema.org', 'w3.org', 'google.com/url', 'support.google.com',
        'fonts.googleapis.com', 'gravatar.com', 'facebook.com', 'twitter.com',
        'instagram.com', 'youtube.com'
    }

    for raw_u in raw_urls:
        u = ats_bridge.clean_url(raw_u)
        netloc = urlparse(u).netloc.lower()
        if not u or u in seen or any(d in netloc for d in ignore_domains):
            continue
        seen.add(u)
        cleaned_urls.append(u)

    return full_text, html_text, cleaned_urls


def classify_message_intent(subject: str, body: str, links: list) -> tuple:
    """
    Classify recruiter email intent, sentiment, and extract actionable cues.
    Returns: (intent, sentiment, is_actionable, task_suggestion)
    """
    text = (subject + " " + body).lower()

    # 0. Promotional, Marketing, Webinar, & Newsletter Checks
    promo_cues = [
        "invitation credits expiring", "savings of up to", "special discount",
        "webinar", "register for free", "join our live session", "newsletter",
        "manage preferences", "to stop receiving these emails", "exclusive offer",
        "inside claude code", "sponsored"
    ]
    if any(q in text for q in promo_cues):
        return "IGNORE", "NEUTRAL", False, None

    # 0.1 Automated Portal Notification / Confirmation Check
    auto_notif_cues = [
        "your application has been submitted", "application submitted successfully",
        "job recommendations for", "jobs matching your profile",
        "manage your job alerts", "unsubscribe from job alerts", "recommended jobs"
    ]
    if any(q in text for q in auto_notif_cues):
        return "APPLICATION_RECEIPT", "NEUTRAL", False, None

    # 1. Rejection Check
    rejection_cues = [
        "not moving forward", "decided to move forward with other", "pursue other candidates",
        "will not be moving forward", "filled the position", "regret to inform",
        "unfortunately, we have decided", "at this time, we will not", "not selected",
        "unable to offer", "will not be progressing", "not advancing", "unsuccessful",
        "decided not to proceed", "interview feedback - strengths and growth areas",
        "strengths and growth areas", "application results", "after careful consideration"
    ]
    if any(q in text for q in rejection_cues):
        return "REJECTION", "NEGATIVE", False, None

    # 1.1 Referral-Only Policy Check (e.g. Zomato / Eternal)
    referral_cues = [
        "only accept applications through employee referrals",
        "only accept applications through referrals",
        "through employee referrals",
        "employee referrals only",
        "find someone in our team who can vouch",
        "vouch for you, and have a conversation",
        "internal referral only",
        "require an internal referral",
        "applications through referrals only"
    ]
    if any(q in text for q in referral_cues):
        return "REFERRAL_ONLY_POLICY", "POSITIVE", True, {
            "type": "REFERRAL_HUNT",
            "title": "Connect with Employees on LinkedIn for Referral",
            "priority": "HIGH"
        }

    # 1.5 Interview Completed / Post-Interview Feedback Check
    interview_completed_cues = [
        "thank you for completing your", "completed your interview", "completing your ai interview",
        "interview feedback", "completed the assessment", "assessment completed"
    ]
    if any(q in text for q in interview_completed_cues):
        return "INTERVIEW_COMPLETED", "NEUTRAL", False, None

    # 2. Form / Portal Application Links
    has_form_link = any(
        "forms.gle" in l or "docs.google.com/forms" in l or "forms.office.com" in l or
        "greenhouse.io" in l or "myworkdayjobs.com" in l or "careers" in l or "apply" in l
        for l in links
    )
    if has_form_link or any(w in text for w in ("fill the form", "submit your details", "apply via link", "application form")):
        return "LINK_PROVIDED", "POSITIVE", True, {
            "type": "APPLY_LINK_MANUAL",
            "title": f"Complete Application Form / Link",
            "priority": "HIGH"
        }

    # 3. Technical Assessment / Take-home
    assessment_cues = ["hackerrank", "codility", "testgorilla", "assessment link", "coding challenge", "take-home test", "online test"]
    if any(q in text for q in assessment_cues):
        return "TECH_ASSESSMENT", "POSITIVE", True, {
            "type": "COMPLETE_ASSESSMENT",
            "title": "Complete Recruiter Technical Assessment",
            "priority": "HIGH"
        }

    # 4. Screening / Interview Call Request
    interview_cues = [
        "schedule a call", "screening call", "quick discussion",
        "available for a call", "convenient time", "time slots", "speak with you",
        "connect over a call", "zoom meeting", "google meet", "telephonic round"
    ]
    is_completed_past = any(p in text for p in ("completed your", "thank you for completing", "feedback on your", "completing your"))
    if not is_completed_past and ("interview with" in text or "interview for" in text):
        interview_cues.append("interview")

    if any(q in text for q in interview_cues) and not is_completed_past:
        return "SCREENING_REQUEST", "POSITIVE", True, {
            "type": "SCHEDULE_CALL",
            "title": "Respond with Screening Call Availability",
            "priority": "HIGH"
        }

    # 5. CTC & Notice Period Details Request
    ctc_cues = ["notice period", "current ctc", "expected ctc", "ectc", "cctc", "lpa", "fixed ctc"]
    if any(q in text for q in ctc_cues):
        return "CTC_NOTICE_QUERY", "POSITIVE", True, {
            "type": "REVIEW_REPLY_DRAFT",
            "title": "Confirm CTC & 7-Day Notice Period",
            "priority": "MEDIUM"
        }

    # 6. Resume / Portfolio Request
    resume_cues = ["updated resume", "share your cv", "latest resume", "attach your profile", "portfolio"]
    if any(q in text for q in resume_cues):
        return "RESUME_REQUEST", "POSITIVE", True, {
            "type": "SEND_DOCUMENTS",
            "title": "Send Updated Resume to Recruiter",
            "priority": "MEDIUM"
        }

    return "GENERAL_INQUIRY", "POSITIVE", False, None


def extract_company_and_role(subject: str, body: str, sender_email: str, existing_entry: dict = None) -> tuple:
    """Deduce company name and job role from email context and domain."""
    company = ""
    role = ""

    if existing_entry:
        company = existing_entry.get("company", "")
        role = existing_entry.get("role", "")

    # Derive company from domain if professional email
    if not company or company in ("Unknown Company", "Company"):
        domain = sender_email.split('@')[-1].lower() if '@' in sender_email else ""
        if domain and domain not in ('gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com'):
            # First check if this domain was an ATS / portal link shared by a known recruiter
            try:
                crm_obj = crm_tracker.load_crm()
                for t_email, t_info in crm_obj.get("threads", {}).items():
                    for ext_l in t_info.get("extracted_links", []):
                        u = ext_l.get("url", "")
                        if domain in u.lower() and t_info.get("company") not in ("Unknown Company", "Company", ""):
                            company = t_info.get("company")
                            role = role or t_info.get("role")
                            break
                    if company and company not in ("Unknown Company", "Company"):
                        break
            except Exception:
                pass

            if not company or company in ("Unknown Company", "Company"):
                domain_name = domain.split('.')[0]
                if len(domain_name) > 2:
                    company = domain_name.replace('-', ' ').replace('_', ' ').title()

    # Extract role from Subject (e.g. "Opening for Java Developer", "Senior Backend Engineer at X")
    if not role or role in ("Candidate", "Software Engineer", ""):
        match_role = re.search(r'(?i)(?:role|position|opening|hiring\s+for|job\s+opportunity)\s*[:\-]?\s*([A-Za-z0-9\s\+\#\/\.\-]{3,40})', subject)
        if match_role:
            role = match_role.group(1).strip()
        else:
            role = "Full Stack / Java Developer"

    return company or "Hiring Company", role or "Full Stack Developer"


def harvest_and_purge_bounced_emails(mail, since_date: str, sender_email: str) -> dict:
    """
    Search Gmail IMAP for hard bounce notices (mailer-daemon, delivery status notifications),
    extract rejected recipient addresses, add them to suppression_list.json, mark BOUNCED in sent_emails.json,
    and permanently delete them from recruiter_crm.json.
    """
    logging.info(f"[Harvester-Bounces] Searching for bounce notices since {since_date}...")
    bounced_found = set()

    search_queries = [
        f'(FROM "mailer-daemon" SINCE "{since_date}")',
        f'(FROM "Mail Delivery Subsystem" SINCE "{since_date}")',
        f'(SUBJECT "Delivery Status Notification" SINCE "{since_date}")',
        f'(SUBJECT "Undelivered Mail Returned to Sender" SINCE "{since_date}")'
    ]

    all_bounce_ids = []
    for q in search_queries:
        try:
            typ, data = mail.search(None, q)
            if typ == "OK" and data and data[0]:
                for mid in data[0].split():
                    if mid not in all_bounce_ids:
                        all_bounce_ids.append(mid)
        except Exception as ex:
            logging.debug(f"[Harvester-Bounces] Query '{q}' error: {ex}")

    if not all_bounce_ids:
        logging.info("[Harvester-Bounces] No bounce messages detected in target date window.")
        return {"bounced_count": 0, "purged_from_crm": 0, "bounced_addresses": []}

    logging.info(f"[Harvester-Bounces] Inspecting {len(all_bounce_ids)} bounce notifications...")

    supp_path = os.path.join(LOGS_DIR, "suppression_list.json")
    sent_path = os.path.join(LOGS_DIR, "sent_emails.json")

    supp_data = {"suppressed_emails": {}, "suppressed_domains": {}}
    if os.path.exists(supp_path):
        try:
            with open(supp_path, 'r', encoding='utf-8') as sf:
                supp_data = json.load(sf)
        except Exception:
            pass

    sent_records = {}
    if os.path.exists(sent_path):
        try:
            with open(sent_path, 'r', encoding='utf-8') as sef:
                sent_records = json.load(sef)
        except Exception:
            pass

    sender_lower = (sender_email or "").strip().lower()
    system_ignore = {'mailer-daemon', 'googlemail.com', 'google.com', 'amazonses.com', 'postmaster'}

    for mid in all_bounce_ids:
        try:
            _, msg_data = mail.fetch(mid, "(BODY.PEEK[TEXT])")
            if not msg_data or not msg_data[0]:
                continue
            raw_text = msg_data[0][1].decode("utf-8", errors="ignore")

            candidates = set()
            for pattern in [
                r'(?i)Final-Recipient:\s*rfc822;\s*<?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>?',
                r'(?i)Original-Recipient:\s*rfc822;\s*<?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>?',
                r'(?i)(?:Address not found|wasn\'t delivered to|failed permanently)\s*.*?<?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>?',
                r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            ]:
                for match in re.findall(pattern, raw_text):
                    m_lower = match.strip().lower()
                    if m_lower == sender_lower or any(ign in m_lower for ign in system_ignore):
                        continue
                    if pattern.startswith('[a-zA-Z0-9'):
                        norm_m = crm_tracker.normalize_email(m_lower)
                        if norm_m not in sent_records and m_lower not in sent_records:
                            continue
                    candidates.add(m_lower)

            for cand in candidates:
                bounced_found.add(cand)
        except Exception as ex:
            logging.debug(f"[Harvester-Bounces] Error fetching bounce msg {mid}: {ex}")

    purged_crm_count = 0
    supp_updated = False
    sent_updated = False

    for b_email in bounced_found:
        norm_b = crm_tracker.normalize_email(b_email)
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Add to suppression_list
        if norm_b not in supp_data.setdefault("suppressed_emails", {}):
            supp_data["suppressed_emails"][norm_b] = {
                "raw_email": b_email,
                "reason": "HARD_BOUNCE_IMAP",
                "created_at": now_ts
            }
            supp_updated = True

        # 2. Update sent_records
        for key in (norm_b, b_email):
            if key in sent_records and isinstance(sent_records[key], dict):
                if sent_records[key].get("status") != "BOUNCED":
                    sent_records[key]["status"] = "BOUNCED"
                    sent_records[key]["bounced_at"] = now_ts
                    sent_updated = True

        # 3. Delete from CRM contacts
        deleted = crm_tracker.delete_contact(b_email, reason="HARD_BOUNCE_IMAP")
        if deleted:
            purged_crm_count += 1

    if supp_updated:
        try:
            with open(supp_path, 'w', encoding='utf-8') as sf:
                json.dump(supp_data, sf, indent=2, sort_keys=True)
        except Exception as ex:
            logging.warning(f"[Harvester-Bounces] Could not save suppression_list.json: {ex}")

    if sent_updated:
        try:
            with open(sent_path, 'w', encoding='utf-8') as sef:
                json.dump(sent_records, sef, indent=2)
        except Exception as ex:
            logging.warning(f"[Harvester-Bounces] Could not save sent_emails.json: {ex}")

    logging.info(f"[Harvester-Bounces] Processed {len(bounced_found)} bounced emails | Purged {purged_crm_count} from CRM.")
    return {
        "bounced_count": len(bounced_found),
        "purged_from_crm": purged_crm_count,
        "bounced_addresses": list(bounced_found)
    }


def harvest_recruiter_emails(lookback_days: int = 14, max_emails: int = 50) -> dict:
    """
    Connects to Gmail via IMAP, discovers recruiter emails, updates CRM,
    creates actionable tasks, applies to links, and queues smart reply drafts.
    """
    config = load_config()
    copilot_cfg = config.get("recruiter_copilot", {})
    if not copilot_cfg.get("enabled", True):
        logging.info("[Harvester] Recruiter Copilot is disabled in config.json.")
        return {"status": "DISABLED", "new_messages": 0}

    email_cfg = config.get("email", {})
    sender_email = email_cfg.get("sender_email")
    app_password = email_cfg.get("sender_app_password")

    if not sender_email or not app_password or app_password == "YOUR_GMAIL_APP_PASSWORD":
        logging.warning("[Harvester] Missing sender_email or sender_app_password in config.json.")
        return {"status": "AUTH_CONFIG_MISSING", "new_messages": 0}

    imap_server = config.get("self_learning", {}).get("imap_server", "imap.gmail.com")
    since_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    logging.info(f"[Harvester] Scanning Gmail INBOX via IMAP (since {since_date})...")

    crm = crm_tracker.load_crm()
    known_threads = crm.get("threads", {})

    processed_count = 0
    new_inbound = []
    links_to_apply = []
    bounce_res = {"purged_from_crm": 0, "bounced_count": 0}

    try:
        mail = imaplib.IMAP4_SSL(imap_server, timeout=20)
        mail.login(sender_email, app_password)
        mail.select("INBOX", readonly=True)

        # 1. Harvest & purge bounced emails to keep recruiter CRM and queues clean
        bounce_res = harvest_and_purge_bounced_emails(mail, since_date, sender_email)

        # Search for messages since specified date
        typ, msg_ids = mail.search(None, f'(SINCE "{since_date}")')
        if typ != "OK" or not msg_ids or not msg_ids[0]:
            logging.info("[Harvester] No messages found in INBOX since target date.")
            mail.logout()
            crm_purge_res = crm_tracker.purge_not_working_emails()
            return {
                "status": "SUCCESS",
                "new_messages": 0,
                "leads": [],
                "links_to_apply": [],
                "bounces_purged": bounce_res.get("purged_from_crm", 0),
                "crm_purged_total": crm_purge_res.get("purged_count", 0)
            }

        id_list = msg_ids[0].split()
        # Scan most recent messages up to max_emails
        target_ids = id_list[-max_emails:]
        logging.info(f"[Harvester] Examining {len(target_ids)} recent messages...")

        for mid in reversed(target_ids):
            typ, data = mail.fetch(mid, "(RFC822)")
            if typ != "OK" or not data or not data[0]:
                continue

            raw_email = data[0][1]
            msg = email_lib.message_from_bytes(raw_email)

            from_hdr = msg.get("From", "")
            recruiter_name, from_email = parse_sender_info(from_hdr)
            norm_email = crm_tracker.normalize_email(from_email)

            if not norm_email or from_email == sender_email:
                continue

            # Check if this is a legitimate careers/people/talent/hiring noreply notice (e.g. Zomato)
            is_careers_noreply = bool(re.search(r'(?i)(careers|talent|hiring|recruiting|people|jobs).*no-?reply', from_email))
            if IGNORED_SENDERS_REGEX.search(from_email) and not is_careers_noreply:
                continue

            if not crm_tracker.is_valid_recruiter_email(from_email):
                continue

            is_supp, _ = crm_tracker.is_suppressed_or_bounced(from_email)
            if is_supp:
                continue

            subject = decode_mime_header(msg.get("Subject", ""))
            message_id = msg.get("Message-ID", "").strip()

            # Check if this email was already ingested for this recruiter thread
            entry = known_threads.get(norm_email)
            if entry and message_id and message_id in entry.get("thread_message_ids", []):
                continue

            body_text, html_text, extracted_urls = extract_body_and_links(msg)

            # Verification: Is this a recruiter or response to our outreach?
            is_known_contact = norm_email in known_threads
            contains_hiring_cues = any(k in (subject + " " + body_text).lower() for k in HIRING_KEYWORDS)

            if not is_known_contact and not contains_hiring_cues:
                continue

            # Classify intent and discover actionable links
            intent, sentiment, is_actionable, task_sugg = classify_message_intent(subject, body_text, extracted_urls)
            if intent in ("APPLICATION_RECEIPT", "IGNORE"):
                continue

            company, role = extract_company_and_role(subject, body_text, from_email, entry)

            # Classify URLs using ats_bridge
            classified_links = []
            for u in extracted_urls:
                ats_type, resolved_u = ats_bridge.find_ats_url_in_text(u, resolve_shorteners=True)
                if ats_type:
                    classified_links.append({"url": resolved_u, "ats_type": ats_type})
                elif any(k in u.lower() for k in ('form', 'apply', 'job', 'career', 'hiring')):
                    classified_links.append({"url": u, "ats_type": "generic_form"})

            # Record in CRM
            updated_entry = crm_tracker.record_incoming_message(
                email=from_email,
                subject=subject,
                body_snippet=body_text[:200],
                message_id=message_id,
                recruiter_name=recruiter_name,
                company=company,
                role=role,
                intent=intent,
                sentiment=sentiment,
                links=classified_links
            )

            # Auto-reconcile tasks and cancel drafts if rejected or interview completed
            if intent in ("REJECTION", "INTERVIEW_COMPLETED"):
                sender_domain = from_email.split('@')[-1].lower() if '@' in from_email else ""
                t_status = "COMPLETED" if intent == "INTERVIEW_COMPLETED" else "DISMISSED"
                task_manager.reconcile_tasks_for_company(
                    company=company,
                    recruiter_email=from_email,
                    domain=sender_domain,
                    target_status=t_status,
                    note=f"Auto-reconciled: {subject}"
                )
                try:
                    import reply_agent
                    reply_agent.cancel_pending_drafts_for_recruiter(
                        recruiter_email=from_email,
                        company=company,
                        reason=f"Application concluded / rejected via email: {subject}"
                    )
                except Exception as ex:
                    logging.debug(f"[Harvester] Draft cancellation note: {ex}")

            # If referral-only policy, register referral hunt in Lead Hunt Queue
            if intent == "REFERRAL_ONLY_POLICY":
                try:
                    import intent_miner
                    hunt_co = company
                    full_context = (subject + " " + body_text).lower()
                    if "zomato" in full_context or "eternal" in full_context:
                        hunt_co = "Zomato"
                    intent_miner.queue_lead_for_hunt(
                        company_name=hunt_co,
                        role=role or "Software Engineer",
                        recruiter_hint="",
                        target_type="REFERRAL_HUNT",
                        primary_tech=["Java 21", "Spring Boot 3", "Microservices", "React 19"],
                        source="email_referral_policy",
                        notes=f"Referral notice from {from_email}: {body_text[:200]}",
                        logs_dir=LOGS_DIR
                    )
                    logging.info(f"[Harvester] 🎯 Queued referral hunt for '{hunt_co}' in Lead Hunt Queue.")
                except Exception as ex:
                    logging.warning(f"[Harvester] Failed to queue referral hunt: {ex}")

            # Auto-create Task if actionable
            if is_actionable and copilot_cfg.get("auto_create_tasks", True):
                t_title = f"{task_sugg['title']} for {company}"
                action_url = classified_links[0]["url"] if classified_links else ""
                task_manager.create_task(
                    title=t_title,
                    task_type=task_sugg["type"],
                    recruiter_email=from_email,
                    company=company,
                    role=role,
                    priority=task_sugg["priority"],
                    description=f"Subject: {subject}\n\nSnippet: {body_text[:300]}",
                    action_url=action_url
                )

            # Track links to auto-apply
            if classified_links and copilot_cfg.get("auto_apply_links", True):
                for cl in classified_links:
                    links_to_apply.append({
                        "recruiter_email": from_email,
                        "company": company,
                        "role": role,
                        "url": cl["url"],
                        "ats_type": cl["ats_type"],
                        "subject": subject,
                        "message_id": message_id
                    })

            new_inbound.append({
                "from_email": from_email,
                "recruiter_name": recruiter_name,
                "company": company,
                "role": role,
                "subject": subject,
                "body": body_text,
                "intent": intent,
                "links": classified_links,
                "message_id": message_id
            })

            processed_count += 1
            logging.info(f"[Harvester] Ingested message from {recruiter_name or from_email} ({company} - {intent})")

        mail.logout()

    except Exception as e:
        logging.error(f"[Harvester] IMAP exception: {e}")
        return {"status": "ERROR", "error": str(e), "new_messages": processed_count}

    # Automatically purge dead/bounced/invalid emails from recruiter CRM to keep it clean
    crm_purge_res = crm_tracker.purge_not_working_emails()

    return {
        "status": "SUCCESS",
        "new_messages": processed_count,
        "leads": new_inbound,
        "links_to_apply": links_to_apply,
        "bounces_purged": bounce_res.get("purged_from_crm", 0),
        "crm_purged_total": crm_purge_res.get("purged_count", 0)
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    res = harvest_recruiter_emails()
    print("\n" + "=" * 60)
    print(f"📥 HARVESTER SWEEP COMPLETE: Found {res['new_messages']} new recruiter messages.")
    print("=" * 60)
