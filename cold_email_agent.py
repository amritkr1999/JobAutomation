#!/usr/bin/env python3
"""
Direct Cold Outreach Engine (cold_email_agent.py)
Automated cold email extraction, entity intelligence, queueing, and safe paced dispatch
for direct recruiter/HR outreach without requiring a job post.
"""

import os
import sys
import re
import json
import time
import glob
import random
import logging
import argparse
from datetime import datetime
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract

# Import core capabilities from agent.py
import agent as email_agent

# Base Paths
BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

# Configure pytesseract binary path if needed
if os.path.exists('/opt/homebrew/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'


def get_logs_dir(config=None):
    if not config:
        config = email_agent.load_config()
    return os.path.expanduser(config.get('paths', {}).get('logs', os.path.join(BASE_DIR, 'logs')))


def setup_cold_logging(logs_dir):
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, 'automation.log')
    handlers = [logging.FileHandler(log_file)]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] [ColdEmail] %(message)s',
        handlers=handlers
    )


# -----------------------------------------------------------------------------
# 1. OCR & TEXT EXTRACTION PIPELINE
# -----------------------------------------------------------------------------

def preprocess_image_for_ocr(image_path):
    """Enhance image contrast and resolution for superior OCR email detection."""
    try:
        with Image.open(image_path) as img:
            # Convert to grayscale
            gray = img.convert('L')
            # Enhance contrast
            enhancer = ImageEnhance.Contrast(gray)
            enhanced = enhancer.enhance(2.0)
            # Resize up if image is small
            if enhanced.width < 1200:
                scale = 1200 / float(enhanced.width)
                new_size = (int(enhanced.width * scale), int(enhanced.height * scale))
                enhanced = enhanced.resize(new_size, Image.Resampling.LANCZOS)
            return enhanced
    except Exception as e:
        logging.warning(f"Could not preprocess image {image_path}: {e}")
        return Image.open(image_path)


def extract_emails_from_image(image_path):
    """Extract and heal email addresses from an image/screenshot using Tesseract OCR."""
    logging.info(f"Extracting cold outreach emails from image: {os.path.basename(image_path)}")
    enhanced_img = preprocess_image_for_ocr(image_path)
    
    # Run pytesseract OCR
    raw_ocr = pytesseract.image_to_string(enhanced_img)
    
    # Also extract raw text for context analysis
    emails = extract_emails_from_text(raw_ocr)
    logging.info(f"Extracted {len(emails)} valid emails from {os.path.basename(image_path)}")
    return emails, raw_ocr


def extract_emails_from_text(text):
    """Extract, heal OCR typos, and deduplicate email addresses from raw text."""
    if not text:
        return []
        
    # Heal common spacing around '@' and '.'
    normalized = re.sub(r'([a-zA-Z0-9._%+-])\s*@\s*([a-zA-Z0-9.-]+)', r'\1@\2', text)
    normalized = re.sub(r'@\s*([a-zA-Z0-9.-]+)\s*\.\s*([a-zA-Z]{2,})', r'@\1.\2', normalized)
    
    # Match standard email pattern
    email_regex = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    raw_candidates = re.findall(email_regex, normalized)
    
    seen = set()
    valid_emails = []
    
    for em in raw_candidates:
        # Strip trailing/leading punctuation
        cleaned = em.strip('.,;:()[]{}<>\"\' ')
        # Apply OCR typo heals (.con -> .com, @gmai.com -> @gmail.com)
        healed = email_agent.heal_ocr_email_typos(cleaned)
        
        # Additional OCR domain typo fixes specific to flyers/screenshots
        domain_corrections = {
            'memermott.com': 'mcmermott.com',
            'nhe.sa': 'nhc.sa',
            'drsulaimanathabib.com': 'drsulaimanalhabib.com',
            'ramelinics.com': 'ramclinics.com'
        }
        for bad_d, good_d in domain_corrections.items():
            if healed.lower().endswith('@' + bad_d):
                healed = healed[:-len(bad_d)] + good_d
                break
                
        lower_em = healed.lower()
        if re.match(r'^[\w.%+-]+@[\w.-]+\.[a-zA-Z]{2,}$', lower_em):
            if lower_em not in seen:
                seen.add(lower_em)
                valid_emails.append(healed)
                
    return valid_emails


def is_email_list_source(text, file_path=""):
    """
    Determine if a file or text content is an Email List / Cold Outreach Lead Sheet
    rather than a standard single Job Description (JD).
    """
    emails = extract_emails_from_text(text)
    if len(emails) >= 2:
        return True
        
    # Check if path is in dedicated cold emails directory or filename contains keywords
    if file_path:
        base = os.path.basename(file_path).lower()
        parent = os.path.basename(os.path.dirname(file_path)).lower()
        if parent == 'cold_emails' or any(k in base for k in ['cold', 'email_list', 'emails', 'leads', 'hr_list']):
            return True

    # Check for list patterns like "1- email@", "2. email@", "• email@"
    list_matches = re.findall(r'(?:\d+[\.\-\)]\s*|•\s*)[a-zA-Z0-9._%+-]+@', text)
    if len(list_matches) >= 1:
        return True

    # Check if text is short and contains at least 1 email without typical JD sections
    jd_indicators = ['job description', 'responsibilities', 'requirements', 'qualifications', 'we are looking for', 'key skills', 'experience required']
    has_jd_terms = any(term in text.lower() for term in jd_indicators)
    if len(emails) == 1 and not has_jd_terms and len(text.split()) < 80:
        return True
        
    return False


# -----------------------------------------------------------------------------
# 2. ENTITY INTELLIGENCE (COMPANY & RECRUITER INFERENCE)
# -----------------------------------------------------------------------------

KNOWN_COMPANY_PATTERNS = {
    'arabianfal': 'Arabianfal',
    'aldawaa': 'Al Dawaa',
    'alojaimi': 'Al Ojaimi Group',
    'rawabiholding': 'Rawabi Holding',
    'dnata': 'Dnata',
    'alarayan': 'Al Arayan',
    'alesayi': 'Alesayi Motors',
    'mcmermott': 'McDermott',
    'aytb': 'AYTB',
    'gtecorp': 'GTE Corp',
    'nesr': 'NESR',
    'farm': 'Farm Superstores',
    'alhumamlaw': 'Al Humam Law',
    'gulfteksaudi': 'Gulftek Saudi',
    'catcosa': 'CATCOSA',
    'tafear': 'Tafear',
    'sraco': 'SRACO Group',
    'atco': 'ATCO',
    'yokogawa': 'Yokogawa',
    'shadeco': 'Shade Corporation',
    'alkafaa': 'Al Kafaa',
    'kbr': 'KBR',
    'batook': 'Batook Specialities',
    'shawarmer': 'Shawarmer',
    'archirodon': 'Archirodon',
    'binajinah': 'Bin Ajinah',
    'nesma': 'Nesma & Partners',
    'abdulla-fouad': 'Abdulla Fouad Group',
    'innosoft': 'Innosoft',
    'startime': 'Star Time',
    'musk': 'Musk',
    'bonyan': 'Bonyan',
    'innovest': 'Innovest',
    'nhc': 'National Housing Company (NHC)',
    'artar': 'ARTAR',
    'darwaemaar': 'Dar Wa Emaar',
    'almasah': 'Al Masah',
    'almoosahospital': 'Al Moosa Specialist Hospital',
    'sghgroup': 'Saudi German Health (SGH Group)',
    'drsulaimanalhabib': 'Dr. Sulaiman Al Habib Medical Group',
    'jhah': 'Johns Hopkins Aramco Healthcare (JHAH)',
    'alahsahospital': 'Al Ahsa Hospital',
    'almanahospital': 'Al Mana General Hospitals',
    'familycare': 'Family Care',
    'ramclinics': 'Ram Clinics',
    'fakeeh': 'Fakeeh Care Group',
    'mouwasat': 'Mouwasat Hospitals',
    'dallah-hospital': 'Dallah Hospital',
    'almurjanhospital': 'Al Murjan Hospital'
}


def infer_company_from_domain(email_address, context_text=""):
    """Infer clean corporate company name from email domain and learned domain database."""
    if not email_address or '@' not in email_address:
        return ""
        
    domain = email_address.split('@', 1)[1].lower().strip()
    
    # Generic domains have no corporate company name
    if domain in email_agent.GENERIC_EMAIL_DOMAINS:
        return ""
        
    # Strip ccTLD (.com.sa, .sa, .com, .net, etc.)
    domain_parts = domain.split('.')
    # Remove standard extensions
    clean_parts = [p for p in domain_parts if p not in ['com', 'sa', 'net', 'org', 'care', 'ae', 'co', 'in', 'io', 'tech']]
    root = clean_parts[0] if clean_parts else domain_parts[0]
    
    # Check manual known pattern dictionary
    for k, name in KNOWN_COMPANY_PATTERNS.items():
        if k in root or root in k:
            return name
            
    # Check learned domains from logs
    learned = email_agent.load_learned_domains()
    if root in learned:
        return learned[root]
        
    # Auto-format camelCase or hyphenated domain roots (e.g. "abdulla-fouad" -> "Abdulla Fouad")
    formatted = re.sub(r'[-_]+', ' ', root)
    # Split compound words if camelCase
    formatted = re.sub(r'([a-z])([A-Z])', r'\1 \2', formatted)
    
    # Common suffix handling
    formatted = re.sub(r'(hospital|clinics|group|holding|services|solutions|tech)$', r' \1', formatted, flags=re.I)
    formatted = ' '.join(word.capitalize() for word in formatted.split() if len(word) > 1)
    
    return formatted.strip()


def extract_cold_recruiter_name(email_address):
    """Extract a human recruiter name from email local-part if personal, or return empty for generic roles."""
    if not email_address or '@' not in email_address:
        return ""
        
    local_part, domain = email_address.split('@', 1)
    local_part = local_part.lower().strip()
    
    # Remove numbers and plus-tags
    local_part = re.sub(r'\+.*$', '', local_part)
    
    # If it is a generic role mailbox, do not guess a name
    if (
        local_part in email_agent.GENERIC_ROLE_MAILBOXES
        or any(local_part.startswith(g) for g in (
            'cv', 'recruitment', 'recruit', 'hr', 'careers', 'career', 'job', 'jobs',
            'wadaef', 'talent', 'info', 'marketing', 'support', 'admin', 'contact'
        ))
    ):
        # Check if it has a period or separator after generic prefix (e.g. "recruitment.amjad" -> "Amjad")
        for sep in ('.', '_', '-'):
            if sep in local_part:
                parts = local_part.split(sep)
                for p in parts[1:]:
                    p_clean = re.sub(r'\d+$', '', p)
                    if p_clean.isalpha() and 2 < len(p_clean) <= 14 and p_clean not in email_agent.GENERIC_ROLE_MAILBOXES:
                        return p_clean.capitalize()
        return ""
        
    # Extract from formatted personal email (e.g. "i.atassi" -> "Atassi", "shubham.kashyap" -> "Shubham")
    for sep in ('.', '_', '-'):
        if sep in local_part:
            tokens = local_part.split(sep)
            for t in tokens:
                t_clean = re.sub(r'\d+$', '', t)
                if t_clean.isalpha() and 2 < len(t_clean) <= 14 and t_clean not in email_agent.GENERIC_ROLE_MAILBOXES:
                    return t_clean.capitalize()
                    
    # Single word personal local-part (e.g. "amjad@...")
    single = re.sub(r'\d+$', '', local_part)
    if single.isalpha() and 2 < len(single) <= 14 and single not in email_agent.GENERIC_ROLE_MAILBOXES:
        return single.capitalize()
        
    return ""


# -----------------------------------------------------------------------------
# 3. SPECULATIVE COLD PITCH COPYWRITING
# -----------------------------------------------------------------------------

def compose_cold_subject(company_name, profile, variant_id="variant_a"):
    """Compose a high-converting, professional cold outreach email subject line."""
    sender_name = profile.get('full_name', 'Amrit Kumar')
    notice = profile.get('notice_period', '7 Days')
    
    if variant_id == "variant_b" and company_name:
        return f"Exploring Engineering Opportunities at {company_name} — {sender_name} (3+ YOE)"
    elif variant_id == "variant_c":
        return f"Full Stack / Backend Engineer — {sender_name} | 3.5 YOE (Available in {notice})"
    else:
        return f"Full Stack Developer (Java 21, Spring Boot 3, React 19) — {sender_name} (Immediate Joiner)"


def compose_cold_email_body(company_name, recruiter_name, profile, variant_id="variant_a"):
    """
    Compose an impactful, polite speculative cold outreach pitch.
    Frames outreach around high-demand engineering value without assuming an active job post.
    """
    if recruiter_name:
        greeting = f"Hi {recruiter_name},"
    elif company_name:
        greeting = f"Dear Hiring Team at {company_name},"
    else:
        greeting = "Dear Hiring Team,"
        
    full_name = profile.get('full_name', 'Amrit Kumar')
    notice = profile.get('notice_period', '7 days')
    phone = profile.get('phone', '+91-8789498334')
    email_addr = profile.get('email', 'amrit.399.ak@gmail.com')
    linkedin = profile.get('linkedin', 'https://linkedin.com/in/AmritKumar')
    github = profile.get('github', 'https://github.com/amritkr1999')
    target_target = f"{company_name}'s engineering team" if company_name else "your engineering team"
    
    if variant_id == "variant_b":
        body = f"""{greeting}

I hope this email finds you well. I am reaching out to proactively explore Full Stack and Backend Engineering opportunities with {target_target}.

Over the past 3.5 years as a Full Stack Developer at Ishavx Technology and Argusoft, I have engineered scalable enterprise microservices, modern frontends, and cloud pipelines:
• Core Technologies: Java 21, Spring Boot 3, Microservices, React 19, Python FastAPI, PostgreSQL, AWS (EC2, S3, RDS).
• Engineering Impact: Reduced API response times by 35% and database query latency by 40% across high-volume production datasets.
• Availability: Immediate Joiner ({notice} Notice Period).

Whether you have active openings or upcoming engineering initiatives, I would welcome the chance to connect for a brief introductory call.

Please find my master resume attached for your review. Thank you for your time and consideration.

Best regards,

{full_name}
Full Stack Developer
Mobile: {phone}
Email: {email_addr}
LinkedIn: {linkedin}
GitHub: {github}"""
    else:
        body = f"""{greeting}

I hope you are doing well. I am writing to express my strong interest in joining {target_target} as a Full Stack / Backend Developer.

With 3+ years of hands-on experience delivering enterprise-grade platforms, here is a quick overview of how I can contribute:
• Technical Specialization: Java 21, Spring Boot 3, Microservices Architecture, React 19, Python FastAPI, PostgreSQL, AWS.
• Measurable Delivery: Designed low-latency RESTful APIs, optimized complex relational queries, and deployed robust containerized microservices.
• Availability: Immediate Joiner ({notice} Notice Period).

I have attached my comprehensive resume for your consideration. I would greatly appreciate the opportunity to discuss how my technical expertise aligns with your team's goals.

Thank you for your time, and I look forward to hearing from you.

Sincerely,

{full_name}
Full Stack Developer
Mobile: {phone}
Email: {email_addr}
LinkedIn: {linkedin}
GitHub: {github}"""
        
    return body


# -----------------------------------------------------------------------------
# 4. QUEUE & STAGING MANAGEMENT (lead_hunt_queue.json / cold_email_queue.json)
# -----------------------------------------------------------------------------

def get_queue_file_path(logs_dir):
    return os.path.join(logs_dir, 'cold_email_queue.json')


def load_cold_queue(logs_dir):
    path = get_queue_file_path(logs_dir)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            logging.warning(f"Could not load cold email queue: {e}")
    return []


def save_cold_queue(queue, logs_dir):
    path = get_queue_file_path(logs_dir)
    os.makedirs(logs_dir, exist_ok=True)
    temp_path = path + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(queue, f, indent=2)
        os.replace(temp_path, path)
        return True
    except Exception as e:
        logging.error(f"Error saving cold email queue: {e}")
        return False


def enqueue_cold_leads(raw_emails, source_info="Manual Input", logs_dir=None, config=None):
    """
    Enqueue unique, non-suppressed email leads into the persistent cold email queue.
    Returns (added_count, skipped_count).
    """
    if not config:
        config = email_agent.load_config()
    logs_dir = logs_dir or get_logs_dir(config)
    
    sender_email = config.get('email', {}).get('sender_email', '')
    suppression_data = email_agent.load_suppression_list(logs_dir, config)
    sent_records = email_agent.load_sent_emails(logs_dir, sender_email=sender_email)
    
    current_queue = load_cold_queue(logs_dir)
    queued_emails = {item.get('email', '').lower() for item in current_queue}
    
    added = 0
    skipped = 0
    
    for em in raw_emails:
        em_clean = email_agent.heal_ocr_email_typos(em.strip())
        lower_em = em_clean.lower()
        
        if not re.match(r'^[\w.%+-]+@[\w.-]+\.[a-zA-Z]{2,}$', lower_em):
            skipped += 1
            continue
            
        # Check if already in queue
        if lower_em in queued_emails:
            logging.debug(f"[ColdQueue] Already queued: {em_clean}")
            skipped += 1
            continue
            
        # Check cooldown & suppression list
        allowed, status_code, detail_msg = email_agent.can_send_email(
            em_clean, config, sent_records, suppression_data, sender_email=sender_email
        )
        if not allowed:
            logging.info(f"[ColdQueue Guard] Skipping {em_clean} ({status_code}): {detail_msg}")
            skipped += 1
            continue
            
        # Infer company and recruiter name
        company = infer_company_from_domain(em_clean)
        recruiter = extract_cold_recruiter_name(em_clean)
        
        lead_item = {
            "email": em_clean,
            "company": company,
            "recruiter_name": recruiter,
            "source": source_info,
            "added_at": datetime.now().isoformat()
        }
        current_queue.append(lead_item)
        queued_emails.add(lower_em)
        added += 1
        
    save_cold_queue(current_queue, logs_dir)
    logging.info(f"[ColdQueue] Enqueued {added} new lead(s), skipped {skipped} lead(s). Total in queue: {len(current_queue)}")
    return added, skipped


# -----------------------------------------------------------------------------
# 5. SAFE DISPATCHER (PACED & THROTTLED)
# -----------------------------------------------------------------------------

def ensure_master_resume(config):
    """Ensure a freshly rendered Master Full Stack Developer Resume PDF exists."""
    paths = config.get('paths', {})
    output_dir = os.path.expanduser(paths.get('output', os.path.join(BASE_DIR, 'output')))
    os.makedirs(output_dir, exist_ok=True)
    resume_path = os.path.join(output_dir, 'AMRIT_KUMAR_Resume.pdf')
    
    profile = config.get('profile', {})
    master_jd_info = {
        "company_name": "",
        "job_title": "Full Stack Developer",
        "key_requirements": [
            "Java 21", "Spring Boot 3", "Microservices", "React 19",
            "Python FastAPI", "PostgreSQL", "AWS", "System Design"
        ]
    }
    email_agent.create_tailored_pdf_resume(resume_path, profile, master_jd_info, jd_text="")
    return resume_path


def dispatch_cold_email_queue(limit=None, dry_run=False, force=False, config=None):
    """
    Safely dispatch queued cold outreach emails with anti-spam rate limiting,
    inter-send delay jitter, and domain-level throttling.
    """
    if not config:
        config = email_agent.load_config()
    logs_dir = get_logs_dir(config)
    setup_cold_logging(logs_dir)
    
    cold_cfg = config.get('cold_email', {})
    if not cold_cfg.get('enabled', True) and not force:
        logging.info("[ColdEmail] Cold email engine is disabled in config.json. Use --force to override.")
        return 0
        
    now = datetime.now()
    current_hour = now.hour
    active_start = cold_cfg.get('active_hours_start', 10)
    active_end = cold_cfg.get('active_hours_end', 18)
    
    if not force and not (active_start <= current_hour < active_end):
        logging.info(
            f"[ColdEmail] Current time ({now.strftime('%H:%M')}) is outside active dispatch window "
            f"({active_start}:00 - {active_end}:00). Leads will remain queued."
        )
        return 0
        
    queue = load_cold_queue(logs_dir)
    if not queue:
        logging.info("[ColdEmail] Queue is empty. No cold leads to dispatch.")
        return 0
        
    profile = config.get('profile', {})
    sender_email = config.get('email', {}).get('sender_email', '')
    suppression_data = email_agent.load_suppression_list(logs_dir, config)
    sent_records = email_agent.load_sent_emails(logs_dir, sender_email=sender_email)
    
    # Check daily limit
    daily_cap = limit or cold_cfg.get('daily_send_limit', 25)
    today_str = now.strftime('%Y-%m-%d')
    sent_today_count = sum(
        1 for r in sent_records.values()
        if str(r.get('sent_at', '')).startswith(today_str) and r.get('application_type') == 'COLD_OUTREACH'
    )
    
    remaining_quota = max(0, daily_cap - sent_today_count)
    if remaining_quota <= 0 and not force:
        logging.info(f"[ColdEmail] Daily cold outreach cap reached ({sent_today_count}/{daily_cap} today). Halting.")
        return 0
        
    logging.info(f"[ColdEmail] Starting dispatch run. Queue size: {len(queue)} | Today's sends: {sent_today_count}/{daily_cap}")
    
    # Track domain sends today
    max_per_domain = cold_cfg.get('max_per_domain_per_day', 2)
    domain_sends_today = {}
    for r in sent_records.values():
        if str(r.get('sent_at', '')).startswith(today_str):
            dom = r.get('recipient_domain', '')
            if dom:
                domain_sends_today[dom] = domain_sends_today.get(dom, 0) + 1
                
    master_resume_path = ensure_master_resume(config)
    attach_cover = cold_cfg.get('attach_cover_letter', False)
    min_delay = cold_cfg.get('min_delay_seconds', 20)
    max_delay = cold_cfg.get('max_delay_seconds', 45)
    
    dispatched = 0
    remaining_queue = []
    
    for item in queue:
        email_addr = item.get('email', '').strip()
        company = item.get('company', '').strip()
        recruiter = item.get('recruiter_name', '').strip()
        
        if dispatched >= remaining_quota and not force:
            remaining_queue.append(item)
            continue
            
        allowed, status_code, detail_msg = email_agent.can_send_email(
            email_addr, config, sent_records, suppression_data, sender_email=sender_email
        )
        if not allowed:
            logging.info(f"[ColdEmail Guard] Skipping {email_addr}: {detail_msg}")
            email_agent.record_skipped_email(logs_dir, email_addr, status_code, sent_records)
            continue
            
        domain = email_addr.split('@', 1)[1].lower() if '@' in email_addr else ''
        if domain and domain not in email_agent.GENERIC_EMAIL_DOMAINS:
            current_domain_count = domain_sends_today.get(domain, 0)
            if current_domain_count >= max_per_domain and not force:
                logging.info(f"[Domain Cap] Domain @{domain} reached daily limit ({current_domain_count}/{max_per_domain}). Keeping in queue.")
                remaining_queue.append(item)
                continue
                
        # Compose Subject & Body
        variant_id = email_agent.select_email_variant(config, logs_dir)
        subject = compose_cold_subject(company, profile, variant_id=variant_id)
        body = compose_cold_email_body(company, recruiter, profile, variant_id=variant_id)
        html_body = email_agent.compose_html_email_body(body, profile)
        
        if dry_run:
            print("\n" + "=" * 60)
            print(f"[DRY RUN COLD EMAIL] -> {email_addr}")
            print(f"Target Company: {company or '(Unspecified)'} | Recruiter: {recruiter or '(Team)'}")
            print(f"Subject: {subject}")
            print(f"Attachments: {os.path.basename(master_resume_path)}")
            print("-" * 60)
            print(body)
            print("=" * 60)
            dispatched += 1
            continue
            
        # Send via SMTP
        logging.info(f"[ColdEmail] Sending to {email_addr} (Company: {company or 'Direct'}, Variant: {variant_id})...")
        sent_ok = email_agent.dispatch_gmail_smtp(
            config,
            email_addr,
            company,
            "Full Stack Developer",
            body,
            master_resume_path,
            pdf_cover_path=master_resume_path if attach_cover else None,
            jd_info={"subject_line_hint": subject},
            html_email_body=html_body
        )
        
        if sent_ok:
            dispatched += 1
            domain_sends_today[domain] = domain_sends_today.get(domain, 0) + 1
            email_agent.record_bandit_send(variant_id, logs_dir)
            email_agent.record_sent_email(
                logs_dir,
                email_addr,
                company or "Cold Outreach Lead",
                "Full Stack Developer",
                sent_records,
                email_variant=variant_id,
                key_requirements=["Java 21", "Spring Boot 3", "Microservices", "React 19", "AWS"]
            )
            # Annotate sent_record with application_type
            norm_key = email_agent.normalize_email_identity(email_addr)
            if norm_key in sent_records:
                sent_records[norm_key]['application_type'] = 'COLD_OUTREACH'
                try:
                    with open(os.path.join(logs_dir, 'sent_emails.json'), 'w', encoding='utf-8') as f:
                        json.dump(sent_records, f, indent=2)
                except Exception:
                    pass
                    
            # Safe inter-email pacing delay
            if dispatched < remaining_quota and not dry_run:
                jitter = random.uniform(min_delay, max_delay)
                logging.info(f"[Pacing Guard] Pausing {jitter:.1f}s before next cold email dispatch...")
                time.sleep(jitter)
        else:
            logging.error(f"[ColdEmail] SMTP failure for {email_addr}. Preserving in queue.")
            remaining_queue.append(item)
            
    if not dry_run:
        save_cold_queue(remaining_queue, logs_dir)
        logging.info(f"[ColdEmail] Dispatch run completed. Sent: {dispatched} | Remaining in queue: {len(remaining_queue)}")
        
    return dispatched


# -----------------------------------------------------------------------------
# 6. INGESTION INTERFACE (FILES, IMAGES, TEXT, DROPZONE)
# -----------------------------------------------------------------------------

def ingest_source(source_path_or_text, config=None, logs_dir=None):
    """
    Ingest emails from any source:
    - Path to image (.png, .jpg, .webp, .jpeg)
    - Path to text file (.txt, .csv, .md, .json)
    - Path to directory
    - Raw string of comma/space/newline-separated emails
    """
    if not config:
        config = email_agent.load_config()
    logs_dir = logs_dir or get_logs_dir(config)
    setup_cold_logging(logs_dir)
    
    extracted_emails = []
    source_label = "Direct Input"
    
    # 1. Check if source is a valid file or directory path
    if isinstance(source_path_or_text, str) and os.path.exists(source_path_or_text):
        path = os.path.abspath(source_path_or_text)
        if os.path.isdir(path):
            files = [f for f in glob.glob(os.path.join(path, '*')) if os.path.isfile(f)]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in ['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff']:
                    em, _ = extract_emails_from_image(f)
                    extracted_emails.extend(em)
                else:
                    try:
                        with open(f, 'r', encoding='utf-8', errors='ignore') as fp:
                            em = extract_emails_from_text(fp.read())
                            extracted_emails.extend(em)
                    except Exception:
                        pass
            source_label = f"Directory: {os.path.basename(path)}"
        else:
            ext = os.path.splitext(path)[1].lower()
            if ext in ['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff']:
                extracted_emails, _ = extract_emails_from_image(path)
            else:
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as fp:
                        extracted_emails = extract_emails_from_text(fp.read())
                except Exception as e:
                    logging.error(f"Could not read {path}: {e}")
            source_label = f"File: {os.path.basename(path)}"
    else:
        # 2. Treat as raw string of emails
        extracted_emails = extract_emails_from_text(str(source_path_or_text))
        source_label = "Raw Text Input"
        
    # Deduplicate extracted list
    unique_emails = list(dict.fromkeys(extracted_emails))
    if not unique_emails:
        logging.warning("No valid emails found in provided source.")
        return 0, 0
        
    return enqueue_cold_leads(unique_emails, source_info=source_label, logs_dir=logs_dir, config=config)


# -----------------------------------------------------------------------------
# 7. CLI COMMAND INTERFACE
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Direct Cold Outreach Engine for Job Automation")
    parser.add_argument('input', nargs='?', help="File path, image path, directory, or raw email string to ingest")
    parser.add_argument('--dispatch', action='store_true', help="Dispatch queued cold outreach emails")
    parser.add_argument('--limit', type=int, default=None, help="Maximum emails to dispatch in this run")
    parser.add_argument('--dry-run', action='store_true', help="Preview pitches and target companies without sending")
    parser.add_argument('--force', action='store_true', help="Bypass active hours and daily limits")
    parser.add_argument('--status', action='store_true', help="Display current cold email queue status")
    
    args = parser.parse_args()
    config = email_agent.load_config()
    logs_dir = get_logs_dir(config)
    setup_cold_logging(logs_dir)
    
    if args.status:
        queue = load_cold_queue(logs_dir)
        print("\n" + "=" * 55)
        print("          COLD EMAIL OUTREACH STATUS")
        print("=" * 55)
        print(f"Total Queued Leads: {len(queue)}")
        if queue:
            print("\nNext 5 Leads in Queue:")
            for i, item in enumerate(queue[:5], 1):
                print(f" {i}. {item.get('email')} -> Company: {item.get('company') or '(Unspecified)'}")
        print("=" * 55 + "\n")
        return
        
    if args.input:
        print(f"[ColdEmail] Ingesting leads from input: {args.input}")
        added, skipped = ingest_source(args.input, config=config, logs_dir=logs_dir)
        print(f"[ColdEmail] Added {added} new lead(s) to queue ({skipped} skipped/duplicates).")
        
    if args.dispatch or (args.input and not args.dry_run and not sys.argv[0].endswith('run_all.py')):
        dispatch_cold_email_queue(limit=args.limit, dry_run=args.dry_run, force=args.force, config=config)
    elif args.dry_run:
        dispatch_cold_email_queue(limit=args.limit or 5, dry_run=True, force=True, config=config)


if __name__ == '__main__':
    main()
