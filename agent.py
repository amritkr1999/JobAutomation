#!/usr/bin/env python3
"""
Job Application Pipeline Agent
Automated local job application tailoring, PDF rendering, and email dispatch using Ollama LLM.
"""

import os
import sys
import glob
import json
import re
import logging
import smtplib
import imaplib
import email as email_lib
import random
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import requests
from PIL import Image
import pytesseract
from pypdf import PdfReader

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
import ctypes
import shutil
import subprocess
import time
import optimizer
import intent_miner

# Configure pytesseract binary path if needed
if os.path.exists('/opt/homebrew/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'

# Base Directories
BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')


def enable_macos_dataless_materialization():
    """Allow background launchd Python process to materialize cloud-only Google Drive FileProvider files."""
    if sys.platform != 'darwin':
        return
    try:
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        # IOPOL_TYPE_DISK (0), IOPOL_SCOPE_PROCESS (0), IOPOL_IMPORTANT (1)
        libc.setiopolicy_np(0, 0, 1)
        # IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES (3), IOPOL_SCOPE_PROCESS (0), IOPOL_MATERIALIZE_DATALESS_FILES_ON (2)
        libc.setiopolicy_np(3, 0, 2)
        # IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES (3), IOPOL_SCOPE_THREAD (1), IOPOL_MATERIALIZE_DATALESS_FILES_ON (2)
        libc.setiopolicy_np(3, 1, 2)
    except Exception:
        pass


enable_macos_dataless_materialization()


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Configuration file missing at {CONFIG_PATH}")
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def setup_logging(logs_dir):
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, 'automation.log')
    handlers = [logging.FileHandler(log_file)]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=handlers
    )


def clean_ocr_noise(text):
    """Strip LinkedIn Promoted sidebar ads, browser chrome, and footer noise from OCR text."""
    if not text:
        return ""
    # Remove multi-line LinkedIn Promoted sidebar ad block ("AMRIT, explore relevant opportunities with <Company>")
    text = re.sub(
        r'(?i)(?:AMRIT\s*,?\s*)?explore\s+relevant\s+opportunities\s+with\s+[^\n]+(?:\n+Get\s+the\s+latest\s+jobs\s+and\s+industry\s+news)?',
        '',
        text
    )
    noisy_line_patterns = [
        r'(?i)^.*explore\s+relevant\s+opportunities\s+with.*$',
        r'(?i)^.*get\s+the\s+latest\s+jobs\s+and\s+industry\s+news.*$',
        r'(?i)^.*claim\s+1\s+free\s+month\s+of\s+premium.*$',
        r'(?i)^.*linkedin\s+corporation\s*©.*$',
        r'(?i)^.*privacy\s*&\s*terms\s+ad\s+choices.*$',
        r'(?i)^.*business\s+services\s+get\s+the\s+linkedin\s+app.*$',
        r'(?i)^.*hackerRank.*youtube.*programming\s+chall.*$',
        r'(?i)^.*linkedin\.com/search/results/.*$',
        r'(?i)^\s*promoted\s*\.*$',
    ]
    cleaned_lines = []
    for line in text.splitlines():
        if any(re.match(pat, line.strip()) for pat in noisy_line_patterns):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def extract_text_from_file(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    text = ""
    logging.info(f"Extracting text from: {os.path.basename(file_path)} ({ext})")
    
    enable_macos_dataless_materialization()

    # Copy to a safe local temporary file using pure binary stream to avoid macOS FileProvider xattr locks (EDEADLK)
    temp_dir = "/tmp/job_processing_temp"
    os.makedirs(temp_dir, exist_ok=True)
    temp_file = os.path.join(temp_dir, f"temp_job_{os.getpid()}_{abs(hash(file_path)) % 100000}{ext}")
    
    for attempt in range(4):
        try:
            with open(file_path, 'rb') as f_in, open(temp_file, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            break
        except Exception as e:
            enable_macos_dataless_materialization()
            if getattr(e, 'errno', None) == 11 and os.path.exists('/usr/bin/brctl'):
                subprocess.run(['/usr/bin/brctl', 'download', file_path], timeout=15, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if attempt < 3:
                time.sleep(2)
            else:
                logging.error(f"Failed to copy {file_path} locally: {e}")
                return ""

    try:
        if ext in ['.png', '.jpg', '.jpeg', '.bmp', '.tiff']:
            with Image.open(temp_file) as image:
                image.load()
                raw_bytes = pytesseract.image_to_string(image, output_type=pytesseract.Output.BYTES)
                text = raw_bytes.decode('utf-8', errors='ignore')
        elif ext == '.pdf':
            reader = PdfReader(temp_file)
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
        elif ext in ['.txt', '.md']:
            with open(temp_file, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        else:
            logging.warning(f"Unsupported file format: {ext}")
    except Exception as e:
        logging.error(f"Error reading extracted file {file_path}: {e}")
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass
                
    return clean_ocr_noise(text)


def query_ollama(config, prompt, system_prompt="You are a professional technical recruiter and career assistant."):
    host = config.get('ollama', {}).get('host', 'http://localhost:11434')
    model = config.get('ollama', {}).get('model', 'llama3.1:8b')
    url = f"{host.rstrip('/')}/api/generate"
    
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False,
        "options": {
            "temperature": 0.2
        }
    }
    
    try:
        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()
        result = response.json()
        return result.get('response', '')
    except Exception as e:
        logging.error(f"Ollama API request failed: {e}")
        return ""

call_ollama = query_ollama

GENERIC_EMAIL_DOMAINS = {
    'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.co.in', 'ymail.com',
    'outlook.com', 'hotmail.com', 'live.com', 'msn.com', 'icloud.com',
    'me.com', 'rediffmail.com', 'protonmail.com', 'proton.me', 'zoho.com',
    'aol.com', 'mail.com', 'gmx.com'
}

INVALID_COMPANY_TERMS = {
    '', 'company', 'hiring company', 'hiring team', 'not specified', 'unspecified',
    'unknown', 'confidential', 'undisclosed', 'n/a', 'na', 'none', 'null',
    'nil', 'client', 'mnc', 'leading mnc', 'reputed mnc', 'it company',
    'multiple', 'stealth', 'hiring'
}

NON_TECH_REQ_PATTERN = re.compile(
    r'(?i)\b(ctc|ectc|lpa|salary|compensation|package|notice\s*period|immediate\s*joiner|'
    r'joiner|location|relocat\w*|shift|rotational|experience|years?|yrs?|fresher\w*|'
    r'resume|cv|apply|share|send|email|pan\b|passport|uan\b|pf\b|epfo|onsite|remote|'
    r'hybrid|wfo|wfh|interview\w*|walk-?in|budget|np\b|days\b)\b'
)


def sanitize_str(val, default=""):
    if isinstance(val, list):
        return ", ".join(str(x).strip() for x in val if str(x).strip()) if val else default
    if not isinstance(val, str):
        return str(val).strip() if val is not None else default
    return val.strip() if val.strip() else default


GENERIC_ROLE_MAILBOXES = {
    'hr', 'careers', 'career', 'jobs', 'job', 'info', 'talent', 'recruitment',
    'recruiter', 'recruit', 'admin', 'apply', 'hiring', 'team', 'contact',
    'support', 'resume', 'resumes', 'cv', 'staffing', 'enquiry', 'inquiry',
    'hello', 'office', 'help', 'mail', 'people', 'work', 'global',
    'talentconnect', 'connect', 'query', 'queries'
}


def heal_ocr_email_typos(email_str):
    """Fix common Tesseract OCR domain typos such as .con -> .com or @gmai.com -> @gmail.com."""
    if not email_str or '@' not in email_str:
        return email_str
    em = email_str.strip('.,;:()[]{}<>\"\' ')
    local_part, domain = em.split('@', 1)
    domain_low = domain.lower()
    ocr_domain_fixes = {
        'gmai.com': 'gmail.com',
        'gmall.com': 'gmail.com',
        'gnail.com': 'gmail.com',
        'gmail.con': 'gmail.com',
        'gmail.co': 'gmail.com',
        'gmal.com': 'gmail.com',
        'yaho.com': 'yahoo.com',
        'yahoo.con': 'yahoo.com',
        'outlok.com': 'outlook.com',
        'outlook.con': 'outlook.com',
        'hotmai.com': 'hotmail.com',
    }
    if domain_low in ocr_domain_fixes:
        domain = ocr_domain_fixes[domain_low]
    else:
        domain = re.sub(r'\.con$', '.com', domain, flags=re.I)
        domain = re.sub(r'\.co\.im$', '.co.in', domain, flags=re.I)
    return f"{local_part}@{domain}"


def extract_valid_emails(raw_val, jd_text="", sender_email=""):
    """Extract all valid email addresses from LLM output with fallback to raw JD text and OCR typo healing."""
    email_regex = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    candidates = []
    if raw_val:
        normalized_raw = re.sub(r'\s*@\s*', '@', str(raw_val))
        candidates.extend(re.findall(email_regex, normalized_raw))
    if not candidates and jd_text:
        normalized_jd = re.sub(r'([a-zA-Z0-9._%+-])\s*@\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', r'\1@\2', jd_text)
        candidates.extend(re.findall(email_regex, normalized_jd))

    seen = set()
    valid = []
    for em in candidates:
        healed = heal_ocr_email_typos(em)
        em_clean = healed.lower()
        if not re.match(r'^[\w.%+-]+@[\w.-]+\.[a-zA-Z]{2,}$', em_clean):
            continue
        if em_clean not in seen:
            seen.add(em_clean)
            valid.append(healed)

    # If multiple emails were found and one is sender_email (e.g. from browser profile), exclude sender_email
    if sender_email and len(valid) > 1:
        filtered = [e for e in valid if e.lower() != sender_email.lower()]
        if filtered:
            valid = filtered

    return valid


def _get_logs_dir():
    try:
        cfg = load_config()
        return os.path.expanduser(cfg.get('paths', {}).get('logs', os.path.join(BASE_DIR, 'logs')))
    except Exception:
        return os.path.join(BASE_DIR, 'logs')


def load_learned_domains(logs_dir=None):
    logs_dir = logs_dir or _get_logs_dir()
    path = os.path.join(logs_dir, 'learned_domains.json')
    default_domains = {
        'deloitte': 'Deloitte',
        'mewartechservices': 'Mewar Tech Services',
        'alphasoftwaretech': 'Alpha Software Tech',
        'peoplefy': 'Peoplefy',
        'infolexus': 'Infolexus Solutions',
        'daiviktech': 'Daiviksoft Technologies',
        'mypcot': 'Mypcot Infotech',
        'dexian': 'Dexian India',
        'isiteinfo': 'iSite Technologies',
        'collaboraitinc': 'CollaboraIT Inc.',
        'voltoconsulting': 'VOLTO Consulting',
        'winxservice': 'Winx Consulting',
        'gracehire': 'Grace Hire',
        'vitimcloud': 'Vitim Cloud',
        'linkageit': 'Linkage IT',
        'noesisit': 'Noesis Infotech',
        'americanconsulting': 'American Consulting',
        'bytebillion': 'ByteBillion',
        'albireorecruiters': 'Albireo Recruiters',
        'perennialsys': 'Perennial Systems',
        'softsages': 'SoftSages Technology',
        'intellicus': 'Intellicus',
        'amritconsultancy': 'Amrit Consultancy',
        'sansidsolutions': 'Sansid Solutions',
        'vysystems': 'VY Systems',
        'hotbitinfosoft': 'Hotbit Infosoft',
        'spectratekshapers': 'Spectratek Shapers',
        'smartek21': 'SmarTek21',
        'etechgs': 'Etech Global Services',
        'nityainc': 'Nitya Inc.',
    }
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    default_domains.update({k.lower(): v for k, v in data.items() if v})
        except Exception:
            pass
    return default_domains


def save_learned_domain(domain_root, company_name, logs_dir=None):
    if not domain_root or not company_name or len(domain_root) < 3:
        return
    logs_dir = logs_dir or _get_logs_dir()
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, 'learned_domains.json')
    domains = load_learned_domains(logs_dir)
    key = domain_root.lower().strip()
    if key not in domains:
        domains[key] = company_name.strip()
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(domains, f, indent=2, sort_keys=True)
            logging.info(f"[Self-Learning] Learned new corporate domain mapping: @{key} -> {company_name.strip()}")
        except Exception as e:
            logging.warning(f"Could not save learned_domains.json: {e}")


def resolve_company_name(raw_company, primary_email="", jd_text="", logs_dir=None):
    """Clean and validate company name, using and updating learned_domains.json automatically."""
    comp = sanitize_str(raw_company, "")
    comp = re.sub(r'\s*\([^)]*(?:not\s+specified|unspecified|unknown|confidential|undisclosed|optional|none|n/a)[^)]*\)', '', comp, flags=re.I).strip()
    comp = comp.strip(' -–—:,.;"\'')

    comp_lower = comp.lower()
    is_invalid = (
        not comp_lower
        or comp_lower in INVALID_COMPANY_TERMS
        or any(phrase in comp_lower for phrase in [
            'not specified', 'unspecified', 'unknown', 'confidential',
            'undisclosed', 'hiring company', 'not mentioned', 'no company'
        ])
    )

    email_domain = ""
    domain_root = ""
    if primary_email and '@' in primary_email:
        email_domain = primary_email.split('@', 1)[1].lower().strip()
        domain_root = email_domain.split('.')[0]

    # Guard against LinkedIn Promoted sidebar ad ("InnoForge") when email is not innoforge
    if comp_lower == 'innoforge' and 'innoforge' not in email_domain:
        is_invalid = True

    learned_domains = load_learned_domains(logs_dir)

    # Guard against LinkedIn poster's personal headline ("<Role> at <PosterCompany>") when email is a different corporate domain
    if not is_invalid and email_domain and email_domain not in GENERIC_EMAIL_DOMAINS:
        comp_tokens = [t for t in re.split(r'\W+', comp_lower) if len(t) >= 3]
        domain_matches_comp = any(t in domain_root or domain_root in t for t in comp_tokens)
        if not domain_matches_comp and jd_text:
            headline_pat = rf'(?i)\b(?:analyst|engineer|developer|manager|consultant|lead|architect|director|specialist)\s+at\s+{re.escape(comp)}\b'
            if re.search(headline_pat, jd_text):
                is_invalid = True

    if is_invalid:
        if email_domain and email_domain not in GENERIC_EMAIL_DOMAINS and len(domain_root) >= 3:
            if domain_root in learned_domains:
                return learned_domains[domain_root]
            return domain_root.capitalize()
        return ""

    # Self-learn new valid corporate domain -> company mappings when tokens align
    if email_domain and email_domain not in GENERIC_EMAIL_DOMAINS and len(domain_root) >= 3:
        comp_tokens = [t for t in re.split(r'\W+', comp_lower) if len(t) >= 3]
        if any(t in domain_root or domain_root in t for t in comp_tokens):
            save_learned_domain(domain_root, comp, logs_dir)

    return comp


def extract_recruiter_first_name(raw_recruiter_name="", recipient_email="", company_name=""):
    """
    Extract a verified, clean human first name for personalized email greeting ('Hi Shubham,').
    
    SAFETY PRINCIPLES:
    1. Corporate email (e.g. shubham.kashyap@zapnixinc.com) is ground truth.
       If an email address yields an unambiguous personal name ('Shubham'), NEVER override
       it with a conflicting LLM extraction from post text (e.g. 'Suman').
    2. Company names or email domains (e.g. 'Peopleons', 'Zapnix') must NEVER be used as human names.
    3. Generic role mailboxes (e.g. 'global@', 'careers@', 'hr@') must never guess unverified names;
       they return empty string so the system addresses them with 'Dear Hiring Team at {Company},'.
    """
    raw_name_clean = ""
    if raw_recruiter_name:
        cleaned = re.sub(
            r'(?i)\b(?:hr|recruiter|mr|ms|mrs|dr|er|sir|maam|madam|team|hiring|manager|lead|director|founder|co-founder|consultant)\b\.?',
            '',
            str(raw_recruiter_name)
        ).strip(' -–—:,.;"\'()[]{}')
        parts = [p for p in cleaned.split() if p.isalpha() and 2 < len(p) <= 14]
        if parts:
            candidate = parts[0].capitalize()
            candidate_low = candidate.lower()
            if (
                candidate_low not in GENERIC_ROLE_MAILBOXES
                and candidate_low not in {'amrit', 'kumar', 'unknown', 'confidential', 'recruiter', 'hiring', 'opportunity', 'developer', 'engineer'}
            ):
                raw_name_clean = candidate

    comp_clean = (company_name or '').strip().lower()
    if raw_name_clean:
        raw_low = raw_name_clean.lower()
        if comp_clean and (raw_low in comp_clean or comp_clean in raw_low):
            logging.info(f"[Recruiter Guard] Discarding recruiter name '{raw_name_clean}' matching company name '{company_name}'")
            raw_name_clean = ""

    email_first_name = ""
    is_generic_email = False
    domain_root = ""
    local_part = ""
    
    if recipient_email and '@' in recipient_email:
        local_part, domain = recipient_email.split('@', 1)
        local_part = local_part.lower().strip()
        local_part = re.sub(r'\+.*$', '', local_part)
        domain = domain.lower().strip()
        domain_root = domain.split('.')[0] if '.' in domain else domain

        if raw_name_clean and raw_name_clean.lower() == domain_root:
            logging.info(f"[Recruiter Guard] Discarding recruiter name '{raw_name_clean}' matching email domain '{domain}'")
            raw_name_clean = ""

        if (
            local_part in GENERIC_ROLE_MAILBOXES
            or any(local_part.startswith(g) for g in ('global', 'talentconnect', 'admin', 'office', 'help', 'info', 'support', 'contact'))
        ):
            is_generic_email = True
        else:
            for sep in ('.', '_', '-'):
                if sep in local_part:
                    token = local_part.split(sep, 1)[0]
                    token = re.sub(r'\d+$', '', token)
                    if token.isalpha() and 2 < len(token) <= 14 and token not in GENERIC_ROLE_MAILBOXES:
                        email_first_name = token.capitalize()
                        break
            
            if not email_first_name and local_part.isalpha() and 2 < len(local_part) <= 14:
                if local_part not in GENERIC_ROLE_MAILBOXES and domain not in ('gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com'):
                    email_first_name = local_part.capitalize()

    # --- RECONCILIATION & CONFLICT RESOLUTION ---
    if email_first_name:
        if raw_name_clean:
            if raw_name_clean.lower() == email_first_name.lower() or raw_name_clean.lower() in local_part:
                return email_first_name
            else:
                logging.warning(
                    f"[Recruiter Guard] Conflict detected: LLM extracted '{raw_name_clean}', "
                    f"but email is '{recipient_email}'. Overriding with verified email name '{email_first_name}'."
                )
                return email_first_name
        return email_first_name

    if is_generic_email:
        logging.info(f"[Recruiter Guard] Generic team email detected ('{recipient_email}'). Using formal team greeting.")
        return ""

    if raw_name_clean:
        if recipient_email and '@' in recipient_email:
            local = recipient_email.split('@', 1)[0].lower()
            if raw_name_clean.lower() in local:
                return raw_name_clean
        return raw_name_clean

    return ""


def clean_key_requirements(raw_reqs):
    """Filter out HR/administrative terms (like Current CTC, Notice Period, Location) from technical requirements."""
    default_skills = ["Java 21", "Spring Boot", "Microservices", "React", "PostgreSQL", "AWS"]
    if not isinstance(raw_reqs, list):
        return default_skills[:4]

    cleaned = []
    for item in raw_reqs:
        s = sanitize_str(item, "").strip('•-* ')
        if not s or len(s) > 45:
            continue
        if NON_TECH_REQ_PATTERN.search(s):
            continue
        cleaned.append(s)

    if len(cleaned) < 2:
        for fallback in default_skills:
            if fallback.lower() not in [c.lower() for c in cleaned]:
                cleaned.append(fallback)
            if len(cleaned) >= 4:
                break

    return cleaned[:4]


def parse_jd_with_ollama(config, jd_text):
    prompt = f"""
Analyze the following Job Description (JD) text and extract key information in valid JSON format.

IMPORTANT RULES:
1. If multiple job posts or profile headers appear in the text, focus ONLY on the job post that contains the application email address.
2. DO NOT use a LinkedIn poster's personal headline (e.g. "Quality Analyst at Thoughtworks") or sidebar ads as "company_name".
3. If the hiring company name is not explicitly mentioned in the job post, infer it from the corporate email domain, or return "" if the email uses gmail.com/yahoo.com/outlook.com. NEVER return "Hiring Company (not specified)" or "Unknown".
4. "key_requirements" MUST ONLY contain technical skills, programming languages, frameworks, databases, or cloud technologies (e.g. "Java", "Spring Boot", "Microservices", "React", "AWS"). NEVER include HR terms like "Current CTC", "Expected CTC", "Notice Period", "Location", or "Experience".

Return ONLY a single valid JSON object with the following fields:
- "company_name": String name of the company hiring (or "" if not specified)
- "job_title": String singular title of the role (e.g. "Java Full Stack Developer")
- "recipient_email": String email address to submit the application to (or "" if not found)
- "recruiter_name": String first name of the recruiter/poster if mentioned in the post (or "" if not mentioned)
- "subject_line_hint": String required email subject line if the post explicitly specifies one (or "" if not specified)
- "key_requirements": Array of top 4 technical skills/technologies requested
- "summary": A brief 1-2 sentence summary of what the job entails

Job Description:
----------------
{jd_text[:4000]}
----------------
JSON output:
"""
    raw_response = query_ollama(config, prompt)
    
    parsed = {}
    try:
        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(0))
        else:
            parsed = json.loads(raw_response)
    except Exception as e:
        logging.warning(f"Failed to parse LLM response as JSON. Falling back to defaults. Error: {e}")
        parsed = {}
        
    sender_email = config.get('email', {}).get('sender_email', '')
    valid_emails = extract_valid_emails(parsed.get('recipient_email', ''), jd_text, sender_email)
    primary_email = valid_emails[0] if valid_emails else ''

    parsed['recipient_emails'] = valid_emails
    parsed['recipient_email'] = primary_email
    parsed['company_name'] = resolve_company_name(parsed.get('company_name'), primary_email, jd_text)
    parsed['job_title'] = clean_job_title(sanitize_str(parsed.get('job_title'), 'Full Stack Developer'))
    parsed['key_requirements'] = clean_key_requirements(parsed.get('key_requirements'))
    raw_recruiter = sanitize_str(parsed.get('recruiter_name'), '')
    parsed['raw_recruiter_name'] = raw_recruiter
    parsed['recruiter_name'] = extract_recruiter_first_name(raw_recruiter, primary_email, company_name=parsed['company_name'])
    parsed['subject_line_hint'] = sanitize_str(parsed.get('subject_line_hint'), '')
        
    return parsed


def load_winning_examples(logs_dir=None):
    logs_dir = logs_dir or _get_logs_dir()
    path = os.path.join(logs_dir, 'winning_examples.json')
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list) and data:
                    return data
        except Exception as e:
            logging.warning(f"Could not read winning_examples.json: {e}")
    return []


def select_few_shot_examples(jd_info, top_k=2, logs_dir=None):
    """Retrieve the top_k closest winning application examples by technical keyword overlap."""
    examples = load_winning_examples(logs_dir)
    if not examples:
        return []
    target_tokens = set(
        re.findall(
            r'\w+',
            f"{jd_info.get('job_title', '')} {' '.join(jd_info.get('key_requirements', []))}".lower()
        )
    )
    scored = []
    for ex in examples:
        ex_tokens = set(
            re.findall(
                r'\w+',
                f"{ex.get('job_title', '')} {' '.join(ex.get('key_requirements', []))}".lower()
            )
        )
        overlap = len(target_tokens & ex_tokens)
        # Boost real recruiter-replied examples slightly over seed examples
        bonus = 1.5 if ex.get('outcome') == 'REPLIED' else 0.0
        scored.append((overlap + bonus, ex))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:top_k]]


def validate_and_refine_llm_text(config, text, text_type, jd_info, fallback_text):
    """
    Phase 1 Quality Gate: Self-critique check ensuring generated text has concrete technical
    content, zero forbidden HR/meta artefacts, and proper length before rendering into PDF.
    """
    cleaned = clean_llm_text(text)
    sl_cfg = config.get('self_learning', {})
    if not sl_cfg.get('quality_gate_enabled', True):
        return cleaned if len(cleaned) >= 40 else fallback_text

    def _passes_rubric(candidate_str):
        if not candidate_str or len(candidate_str) < 70 or len(candidate_str) > 700:
            return False, "Length outside 70-700 chars"
        low = candidate_str.lower()
        forbidden = [
            'current ctc', 'expected ctc', 'notice period', 'here is', 'this paragraph',
            '[company]', '<company>', 'insert company', 'dear hiring'
        ]
        if any(f in low for f in forbidden):
            return False, "Contains forbidden meta/HR phrase"
        if 'innoforge' in low and (jd_info.get('company_name') or '').lower() != 'innoforge':
            return False, "Contains sidebar ad hallucination (InnoForge)"
        tech_terms = ['java', 'spring', 'react', 'python', 'aws', 'postgres', 'microservices', 'api', 'sql', 'docker', 'kafka']
        if not any(t in low for t in tech_terms):
            return False, "Missing core technical keywords"
        return True, "OK"

    ok, reason = _passes_rubric(cleaned)
    if ok:
        return cleaned

    logging.info(f"[Quality Gate] Regenerating {text_type} ({reason})...")
    repair_prompt = f"""
Rewrite the following {text_type} for Amrit Kumar (Full Stack Developer, 3+ YOE in Java 21, Spring Boot 3, Microservices, React 19, Python FastAPI, PostgreSQL, AWS).
Target Role: {jd_info.get('job_title')}
Target Technical Skills: {', '.join(jd_info.get('key_requirements', []))}

STRICT REQUIREMENTS:
- 2 to 3 concise, high-impact sentences (80 to 450 characters total).
- Include concrete metrics (e.g. 35% faster server response times, 40% lower DB query latency, or 65% faster query speeds).
- Mention ONLY technical skills and engineering achievements. NEVER mention CTC, salary, or notice period.
- Output ONLY the final text with no preamble or quotes.
"""
    repaired = clean_llm_text(query_ollama(config, repair_prompt))
    ok2, _ = _passes_rubric(repaired)
    if ok2:
        return repaired
    return fallback_text


def generate_tailored_paragraph(config, jd_info, jd_text):
    company_display = jd_info.get('company_name') or 'your organization'
    few_shot_block = ""
    if config.get('self_learning', {}).get('few_shot_enabled', True):
        examples = select_few_shot_examples(jd_info, top_k=2)
        if examples:
            formatted_ex = []
            for idx, ex in enumerate(examples, 1):
                if ex.get('alignment_paragraph'):
                    formatted_ex.append(
                        f"Example {idx} (Role: {ex.get('job_title')}, Tech: {', '.join(ex.get('key_requirements', []))}):\n{ex.get('alignment_paragraph')}"
                    )
            if formatted_ex:
                few_shot_block = "\nHigh-Performing Reference Examples:\n" + "\n\n".join(formatted_ex) + "\n"

    prompt = f"""
Candidate: Amrit Kumar (Full Stack Developer, 3+ years experience).
Core Tech: Java 21, Spring Boot 3, Hibernate/JPA, Microservices Architecture, React 19, Python FastAPI, PostgreSQL, Docker, AWS, Kafka.
Key Achievements: Reduced server response times by 35% & DB query latency by 40%, built ABDM-compliant health platforms (Medplat, AROG), B2B procurement platform (VendorInfra), and AI SaaS platform (EasyNaukri).
{few_shot_block}
Target Job:
Company: {company_display}
Role: {jd_info.get('job_title')}
Technical Requirements: {', '.join(jd_info.get('key_requirements', []))}

Write 1 single concise, confident 2-3 sentence alignment paragraph written in the first person ("I").
STRICT RULES:
- Focus strictly on technical skills and quantified engineering achievements (e.g., 35% response time reduction, 40% latency reduction). NEVER mention CTC, salary, or notice period.
- DO NOT wrap the paragraph in quotes.
- DO NOT include conversational preamble or postscripts.
- Return ONLY the paragraph text itself.
"""
    alignment_text = query_ollama(config, prompt)
    fallback_alignment = (
        f"In my recent engineering roles at Tech Solutions Inc. and Global Software Systems, I architected production Spring Boot 3 microservices, "
        f"React 19 interfaces, and high-volume PostgreSQL databases—reducing server response times by 35% and query execution latency by 40%. "
        f"My hands-on experience with {', '.join(jd_info.get('key_requirements', ['Java 21', 'Spring Boot', 'AWS'])[:3])} positions me to add immediate technical value to {company_display}."
    )
    return validate_and_refine_llm_text(config, alignment_text, "cover letter alignment paragraph", jd_info, fallback_alignment)


def render_pdf_document(output_pdf_path, title, header_info, sections):
    doc = SimpleDocTemplate(
        output_pdf_path,
        pagesize=letter,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )
    
    styles = getSampleStyleSheet()
    
    name_style = ParagraphStyle(
        'DocName',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#1A2B4C'),
        alignment=0
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#4A5568')
    )
    
    section_heading = ParagraphStyle(
        'SectionHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#1A2B4C'),
        spaceBefore=10,
        spaceAfter=4
    )
    
    body_style = ParagraphStyle(
        'BodyTextCustom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13.5,
        textColor=colors.HexColor('#2D3748')
    )
    
    bullet_style = ParagraphStyle(
        'BulletCustom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13,
        leftIndent=12,
        textColor=colors.HexColor('#2D3748')
    )

    story = []
    
    story.append(Paragraph(header_info.get('name', 'Amrit Kumar'), name_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph(header_info.get('subtitle', ''), subtitle_style))
    story.append(Paragraph(header_info.get('contact', ''), subtitle_style))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#1A2B4C'), spaceAfter=12))
    
    for sec_title, sec_content in sections:
        if sec_title:
            story.append(Paragraph(sec_title.upper(), section_heading))
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#CBD5E0'), spaceAfter=6))
        
        if isinstance(sec_content, list):
            for item in sec_content:
                clean_item = item.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                if clean_item.startswith('•') or clean_item.startswith('-'):
                    story.append(Paragraph(f"• {clean_item.lstrip('•- ')}", bullet_style))
                    story.append(Spacer(1, 2))
                else:
                    story.append(Paragraph(clean_item, body_style))
                    story.append(Spacer(1, 4))
        else:
            clean_text = sec_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br/>')
            story.append(Paragraph(clean_text, body_style))
            story.append(Spacer(1, 6))
            
    doc.build(story)


def generate_tailored_resume_summary(config, jd_info, jd_text):
    fallback_summary = (
        "Full Stack Developer with 3+ years of experience architecting and delivering high-performance enterprise applications, "
        "microservices, and AI-driven platforms. Skilled in Java 21 (Spring Boot 3, Hibernate, JPA), Python (FastAPI), React 19, "
        "and PostgreSQL, with a proven track record of reducing server response times by 35% and database query latency by 40% on AWS."
    )
    if not jd_text or not jd_text.strip():
        return fallback_summary

    few_shot_block = ""
    if config.get('self_learning', {}).get('few_shot_enabled', True):
        examples = select_few_shot_examples(jd_info, top_k=2)
        if examples:
            formatted_ex = []
            for idx, ex in enumerate(examples, 1):
                if ex.get('resume_summary'):
                    formatted_ex.append(
                        f"Example {idx} (Role: {ex.get('job_title')}, Tech: {', '.join(ex.get('key_requirements', []))}):\n{ex.get('resume_summary')}"
                    )
            if formatted_ex:
                few_shot_block = "\nHigh-Performing Reference Summaries:\n" + "\n\n".join(formatted_ex) + "\n"

    prompt = f"""
Candidate: Amrit Kumar (Full Stack Developer, 3+ years experience).
Core Tech: Java 21, Spring Boot 3, Hibernate/JPA, Microservices Architecture, React 19, Python FastAPI, PostgreSQL, Docker, AWS, Kafka.
Key Metrics: Reduced server response times by 35%, DB query latency by 40%, and skill-matching query speeds by 65%.
{few_shot_block}
Target Job Info:
Role: {jd_info.get('job_title')}
Key Technical Requirements: {', '.join(jd_info.get('key_requirements', []))}

Write a 2-3 sentence tailored Professional Summary for Amrit's resume that directly aligns his technical expertise and quantified achievements with these requirements.
STRICT RULES:
- Focus strictly on software engineering, architecture, and technical skills. NEVER mention CTC, salary, notice period, or the target company's name in the resume summary.
- DO NOT include any conversational phrase like 'Here is a summary'. Output ONLY the summary text directly.
"""
    summary = query_ollama(config, prompt)
    return validate_and_refine_llm_text(config, summary, "resume professional summary", jd_info, fallback_summary)

def extract_json_dict(text):
    if not text:
        return {}
    clean_text = re.sub(r'```json\s*', '', text)
    clean_text = re.sub(r'```\s*', '', clean_text).strip()
    start = clean_text.find('{')
    end = clean_text.rfind('}')
    if start != -1 and end != -1:
        snippet = clean_text[start:end+1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}

def generate_tailored_skills(config, jd_info):
    key_reqs = jd_info.get('key_requirements', [])
    req_str = ", ".join(key_reqs) if isinstance(key_reqs, list) else str(key_reqs)
    
    prompt = f"""
Candidate Core Skills:
- Programming Languages: Java 21 (LTS), Python 3.9+, TypeScript, JavaScript (ES6+), SQL, HTML5, CSS3, C++
- Backend & Frameworks: Spring Boot 3, Spring Security, JPA / Hibernate, RESTful APIs, Microservices, HikariCP, Kafka, Node.js, JWT
- AI & Data Engineering: Python FastAPI, AsyncIO, Ollama (Deepseek Vision OCR, Llama 3.1:8b), Pydantic v2, Google Gemini API
- Frontend & Mobile: React 19, React.js, Redux, Angular, TypeScript, Vite, React Native (Expo SDK 56), Tailwind CSS
- Databases & Caching: PostgreSQL 17 (Query Optimization, JPA Mappings), MySQL, MongoDB, Redis 7 Caching
- Cloud & DevOps: AWS (EC2, S3, RDS, CloudWatch, IAM), Oracle Cloud (OCI), Docker, Docker Compose, GitHub Actions CI/CD

Target Job Key Requirements: {req_str}

Re-order the 6 skill categories so that the categories most relevant to "{req_str}" appear first.
Return ONLY valid JSON mapping category titles to skill strings. Example:
{{
  "Backend & Frameworks": "Spring Boot 3, Spring Security, JPA / Hibernate, RESTful APIs, Microservices, HikariCP, Kafka, Node.js, JWT",
  "Cloud & DevOps": "AWS (EC2, S3, RDS, CloudWatch, IAM), Oracle Cloud (OCI), Docker, Docker Compose, GitHub Actions CI/CD",
  "Programming Languages": "Java 21 (LTS), Python 3.9+, TypeScript, JavaScript (ES6+), SQL, HTML5, CSS3, C++",
  "Frontend & Mobile": "React 19, React.js, Redux, Angular, TypeScript, Vite, React Native (Expo SDK 56), Tailwind CSS",
  "Databases & Caching": "PostgreSQL 17 (Query Optimization, JPA Mappings), MySQL, MongoDB, Redis 7 Caching",
  "AI & Data Engineering": "Python FastAPI, AsyncIO, Ollama (Deepseek Vision OCR, Llama 3.1:8b), Pydantic v2, Google Gemini API"
}}
"""
    raw = query_ollama(config, prompt)
    parsed = extract_json_dict(raw)
    if parsed and len(parsed) >= 4:
        return parsed
        
    return {
        "Backend & Frameworks": "Spring Boot 3, Spring Security, JPA / Hibernate, RESTful APIs, Microservices, HikariCP, Kafka, Node.js, JWT",
        "Cloud & DevOps": "AWS (EC2, S3, RDS, CloudWatch, IAM), Oracle Cloud (OCI), Docker, Docker Compose, GitHub Actions CI/CD, Git",
        "Programming Languages": "Java 21 (LTS), Python 3.9+, TypeScript, JavaScript (ES6+), SQL, HTML5, CSS3, C++",
        "Frontend & Mobile": "React 19, React.js, Redux, Angular, TypeScript, Vite, React Native (Expo SDK 56), Tailwind CSS",
        "Databases & Caching": "PostgreSQL 17 (Query Optimization, JPA Mappings), MySQL, MongoDB, Redis 7 Caching",
        "AI & Data Engineering": "Python FastAPI, AsyncIO, Ollama (Deepseek Vision OCR, Llama 3.1:8b), Pydantic v2, Google Gemini API"
    }

def extract_json_array(text):
    if not text:
        return []
    clean_text = re.sub(r'```json\s*', '', text)
    clean_text = re.sub(r'```\s*', '', clean_text).strip()
    start = clean_text.find('[')
    end = clean_text.rfind(']')
    if start != -1 and end != -1:
        snippet = clean_text[start:end+1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []

def select_tailored_projects(config, jd_info, jd_text=""):
    all_projects = [
        {
            "id": "easynaukri",
            "title": "<b>Auto Apply Pro (EasyNaukri) — AI Job Outreach & SaaS Platform</b> (Full Stack Lead & Architect)",
            "bullets": [
                "Multi-Tier Monorepo Architecture: Spring Boot 3 REST API, Python FastAPI AI engine, React 19 Vite web app, React Native Expo mobile app, Chrome Extension.",
                "Async AI & Vision OCR Pipeline: 5-stage Python AI pipeline using Ollama (Deepseek Vision OCR & Llama 3.1:8b) and Gemini API fallback.",
                "Database Optimization: Refactored database schemas and configured HikariCP connection pooling, improving skill-matching query speeds by 65%."
            ]
        },
        {
            "id": "vendorinfra",
            "title": "<b>VendorInfra — B2B Vendor & Contractor Procurement Platform</b> (Solution Architect & Full Stack Developer)",
            "bullets": [
                "B2B Procurement Platform: Spearheaded end-to-end development for the Indian construction sector using Spring Boot 3, React 19, and PostgreSQL.",
                "Cloud Infrastructure: Architected solution schema and managed AWS infrastructure (S3, RDS) with automated deployment pipelines and secure DB backups."
            ]
        },
        {
            "id": "medplat",
            "title": "<b>Medplat — National Health Data Platform</b> (Full Stack Developer)",
            "bullets": [
                "Public Health Infrastructure: Developed a centralized national-scale platform for secure storage and retrieval of millions of public medical records.",
                "High-Throughput Performance: Optimized PostgreSQL queries and JPA indexing, reducing server response times by 35% and query execution latency by 40%."
            ]
        },
        {
            "id": "arog",
            "title": "<b>AROG — Full Stack Health Management System</b> (Full Stack Developer)",
            "bullets": [
                "Clinical Workflow Digitization: Architected ABDM-compliant Health Management System integrating real-time telemedicine and secure EHR management.",
                "Interoperable Architecture: Scaled clinical workflow modules across clinics in Gujarat with strict data integrity and high availability."
            ]
        }
    ]
    
    prompt = f"""
Given 4 projects:
1. easynaukri (AI SaaS, Spring Boot 3, FastAPI, React 19, Ollama, Chrome Extension)
2. vendorinfra (B2B Procurement, Spring Boot, React, PostgreSQL, AWS S3/RDS)
3. medplat (National Data Platform, PostgreSQL query optimization, public medical records)
4. arog (ABDM Health Management System, Telemedicine, EHR management)

Target Job:
Company: {jd_info.get('company_name') or 'Hiring Organization'}
Role: {jd_info.get('job_title')}
Requirements: {', '.join(jd_info.get('key_requirements', []))}

Return a JSON array of the top 3 project IDs in order of relevance for this job. Example: ["easynaukri", "vendorinfra", "medplat"]
"""
    raw = query_ollama(config, prompt)
    selected_ids = extract_json_array(raw)
    if selected_ids and len(selected_ids) >= 2:
        proj_dict = {p["id"]: p for p in all_projects}
        selected_projs = [proj_dict[pid] for pid in selected_ids if pid in proj_dict]
        if selected_projs:
            return selected_projs
        
    return all_projects[:3]

def select_tailored_experience_bullets(jd_info):
    """Dynamically reorder experience bullets under each company so the most JD-relevant achievements lead."""
    target_text = f"{jd_info.get('job_title', '')} {' '.join(jd_info.get('key_requirements', []))}".lower()

    exp1_pool = [
        (
            ["spring", "react", "postgres", "fullstack", "full stack", "b2b", "java"],
            "Spearheaded end-to-end development of VendorInfra, a B2B vendor/contractor procurement platform using Spring Boot, React, and PostgreSQL."
        ),
        (
            ["aws", "cloud", "s3", "rds", "devops", "ci/cd", "docker", "architecture", "schema"],
            "Architected solution schema and managed cloud infrastructure on AWS (S3, RDS), configuring automated deployment pipelines and secure backups."
        ),
        (
            ["ec2", "aws", "cloudwatch", "postgres", "rds", "infrastructure", "monitoring"],
            "Provisioned AWS EC2 app servers, RDS PostgreSQL databases, and S3 document media storage with CloudWatch health monitoring."
        ),
    ]

    exp2_pool = [
        (
            ["postgres", "sql", "jpa", "hibernate", "performance", "latency", "backend", "java", "optimization"],
            "Reduced server response times by 35% and query execution latency by 40% via PostgreSQL query tuning and JPA indexing."
        ),
        (
            ["microservices", "backend", "data", "scale", "high-throughput", "java", "spring"],
            "Engineered high-throughput data processing routines for Medplat (National Health Data Platform), managing millions of public medical records."
        ),
        (
            ["fullstack", "full stack", "react", "healthcare", "abdm", "workflow", "api"],
            "Co-developed AROG, an ABDM-compliant health management platform for clinics across Gujarat, digitizing clinical workflows and EHR data interchange."
        ),
        (
            ["ai", "llm", "python", "fastapi", "automation", "productivity", "tooling"],
            "Leveraged AI-assisted development tooling (Antigravity), improving feature delivery performance and engineering throughput by 30%."
        ),
        (
            ["lead", "senior", "mentor", "review", "junit", "testing", "agile"],
            "Mentored and led a 3-member engineering team through code reviews, standard design patterns, and automated unit testing."
        ),
    ]

    def _rank(pool):
        scored = []
        for idx, (tags, bullet) in enumerate(pool):
            hits = sum(1 for t in tags if t in target_text)
            scored.append((-hits, idx, bullet))
        scored.sort()
        return [b for _, _, b in scored]

    return _rank(exp1_pool), _rank(exp2_pool)


def build_adaptive_subtitle(jd_info):
    role = clean_job_title(jd_info.get('job_title', 'Full Stack Developer'))
    core_stack = ["Java 21", "Spring Boot 3", "Microservices", "React 19", "Python FastAPI", "AWS"]
    reqs_lower = " ".join(jd_info.get('key_requirements', [])).lower()
    if "kafka" in reqs_lower and "Kafka" not in core_stack:
        core_stack.insert(3, "Kafka")
    if "postgres" in reqs_lower and "PostgreSQL" not in core_stack:
        core_stack.insert(4, "PostgreSQL")
    return f"{role} | {' • '.join(core_stack[:5])}"


def create_tailored_pdf_resume(output_path, profile, jd_info, jd_text=""):
    """Render a tailored 1-page ATS PDF resume via resume_engine."""
    from resume_engine import compile_pdf_resume
    from resume_editor import create_tailored_resume_data

    config = load_config()
    tailored_summary = generate_tailored_resume_summary(config, jd_info, jd_text)

    role = clean_job_title(jd_info.get('job_title', 'Full Stack Developer'))
    company = jd_info.get('company_name', '')
    reqs = jd_info.get('key_requirements', [])

    tailored = create_tailored_resume_data(
        company_name=company,
        role_title=role,
        highlight_skills=reqs,
        jd_text=jd_text,
        summary_override=tailored_summary if tailored_summary and len(tailored_summary) >= 30 else ""
    )
    if profile:
        for k in ('full_name', 'phone', 'email', 'linkedin', 'github', 'location', 'notice_period'):
            if profile.get(k):
                tailored['personal_info'][k] = profile[k]

    compile_pdf_resume(tailored, output_path, single_page=True)
    return tailored.get('summary', '')


def is_unspecified_company(company_name):
    if not company_name:
        return True
    low = company_name.strip().lower()
    if low in ('', 'company', 'hiring team', 'hiring company', 'not specified', 'unknown', 'n/a', 'na', 'none', 'confidential'):
        return True
    if 'not specified' in low or 'not mentioned' in low or 'unknown' in low:
        return True
    return False


def create_tailored_pdf_cover_letter(output_path, profile, jd_info, alignment_paragraph):
    """Render a unified, executive 1-page Cover Letter matching the Resume's typography and citing the top matched project."""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=28,
        bottomMargin=28
    )
    styles = getSampleStyleSheet()
    name_style = ParagraphStyle(
        'CLName', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=18, leading=21,
        textColor=colors.HexColor('#1A2B4C')
    )
    subtitle_style = ParagraphStyle(
        'CLSub', parent=styles['Normal'],
        fontName='Helvetica', fontSize=8.5, leading=11,
        textColor=colors.HexColor('#4A5568')
    )
    contact_style = ParagraphStyle(
        'CLContact', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=8, leading=10,
        textColor=colors.HexColor('#2D3748')
    )
    body_style = ParagraphStyle(
        'CLBody', parent=styles['Normal'],
        fontName='Helvetica', fontSize=9.5, leading=14,
        textColor=colors.HexColor('#2D3748'), spaceAfter=8
    )
    bold_body_style = ParagraphStyle(
        'CLBoldBody', parent=body_style,
        fontName='Helvetica-Bold', textColor=colors.HexColor('#1A2B4C')
    )

    date_str = datetime.now().strftime("%B %d, %Y")
    raw_company = (jd_info.get('company_name') or '').strip()
    has_company = not is_unspecified_company(raw_company)
    role = clean_job_title(jd_info.get('job_title', 'Full Stack Developer'))
    recruiter_name = (jd_info.get('recruiter_name') or '').strip()

    if has_company:
        to_lines = f"<b>To:</b> {recruiter_name or 'The Hiring Team / Hiring Manager'}, {raw_company}"
        opening_sentence = f"I am writing to express my strong interest in the <b>{role}</b> position at <b>{raw_company}</b>."
        org_possessive = f"{raw_company}'s"
    else:
        to_lines = f"<b>To:</b> {recruiter_name or 'The Hiring Team / Hiring Manager'}"
        opening_sentence = f"I am writing to express my strong interest in the <b>{role}</b> position with your organization."
        org_possessive = "your organization's"

    salutation = f"Dear {recruiter_name}," if recruiter_name else "Dear Hiring Manager,"

    # Select dynamic project highlight based on JD requirements
    reqs_lower = " ".join(jd_info.get('key_requirements', [])).lower()
    if any(k in reqs_lower for k in ['python', 'fastapi', 'ai', 'llm']):
        project_highlight = (
            "In addition to my enterprise work at Tech Solutions Inc. and Global Software Systems, I architected <b>Auto Apply Pro (EasyNaukri)</b>—a multi-tier platform "
            "combining a Spring Boot 3 REST backend, an asynchronous Python FastAPI AI pipeline, and a React 19 frontend, improving database query speeds by 65%."
        )
    elif any(k in reqs_lower for k in ['react', 'frontend', 'full stack', 'fullstack', 'aws']):
        project_highlight = (
            "In my recent roles at Tech Solutions Inc. and Global Software Systems, I spearheaded <b>VendorInfra</b> (a B2B procurement platform built with Spring Boot 3, "
            "React 19, PostgreSQL, and AWS EC2/RDS/S3) and scaled <b>Medplat</b> (a national health data platform serving millions of records), cutting API response times by 35% and query latency by 40%."
        )
    else:
        project_highlight = (
            "In my recent engineering roles at Tech Solutions Inc. and Global Software Systems, I specialized in architecting high-throughput Spring Boot microservices, "
            "RESTful APIs, and relational database schemas (PostgreSQL, MySQL) for national-scale platforms like <b>Medplat</b> and <b>VendorInfra</b>—reducing server response times by 35% and query latency by 40%."
        )

    email_addr = profile.get('email', 'candidate@example.com')
    linkedin_url = profile.get('linkedin', 'https://linkedin.com/in/yourprofile')
    github_url = profile.get('github', 'https://github.com/yourusername')

    story = []
    story.append(Paragraph(profile.get("full_name", "AMRIT KUMAR").upper(), name_style))
    story.append(Spacer(1, 2))
    story.append(Paragraph(build_adaptive_subtitle(jd_info), subtitle_style))
    story.append(Paragraph(
        f"Mobile: {profile.get('phone')} | Email: <a href=\"mailto:{email_addr}\" color=\"#1A2B4C\">{email_addr}</a> | Notice Period: {profile.get('notice_period', '7 days')}",
        contact_style
    ))
    story.append(Paragraph(
        f"LinkedIn: <a href=\"{linkedin_url}\" color=\"#1A2B4C\">{linkedin_url}</a> | GitHub: <a href=\"{github_url}\" color=\"#1A2B4C\">{github_url}</a>",
        subtitle_style
    ))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor('#1A2B4C'), spaceAfter=10, spaceBefore=0))

    story.append(Paragraph(f"<b>Date:</b> {date_str}", body_style))
    story.append(Paragraph(to_lines, body_style))
    story.append(Paragraph(f"Subject: Application for {role} Position — {profile.get('full_name', 'Amrit Kumar')}", bold_body_style))
    story.append(Paragraph(salutation, body_style))

    story.append(Paragraph(
        f"{opening_sentence} With over 3 years of professional experience building scalable enterprise web applications, "
        f"high-throughput backend microservices, and modern frontends using Java 21, Spring Boot 3, Hibernate/JPA, React 19, "
        f"Python FastAPI, PostgreSQL, and AWS, I am confident in my ability to hit the ground running and add immediate value to your engineering team.",
        body_style
    ))
    story.append(Paragraph(project_highlight, body_style))

    safe_alignment = alignment_paragraph.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    story.append(Paragraph(safe_alignment, body_style))

    story.append(Paragraph(
        f"As an immediate/short-notice joiner (Notice Period: {profile.get('notice_period', '7 days')}), I am eager to contribute to {org_possessive} "
        f"engineering initiatives and solve complex scaling challenges. I have attached my tailored resume for your review. Thank you for your time and consideration.",
        body_style
    ))

    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"Sincerely,<br/><br/><b>{profile.get('full_name', 'Amrit Kumar')}</b><br/>"
        f"Phone: {profile.get('phone')}<br/>"
        f"Email: <a href=\"mailto:{email_addr}\" color=\"#1A2B4C\">{email_addr}</a><br/>"
        f"LinkedIn: <a href=\"{linkedin_url}\" color=\"#1A2B4C\">{linkedin_url}</a><br/>"
        f"GitHub: <a href=\"{github_url}\" color=\"#1A2B4C\">{github_url}</a>",
        body_style
    ))

    doc.build(story)


def clean_llm_text(text):
    if not text:
        return ""
    lines = text.strip().split('\n')
    filtered_lines = []
    for line in lines:
        l_strip = line.strip()
        l_lower = l_strip.lower()
        if not l_strip:
            continue
        if any(l_lower.startswith(prefix) for prefix in [
            "here's", "here is", "sure,", "certainly,", "below is", "this is", "paragraph:", "note:",
            "this paragraph", "the above paragraph", "the paragraph", "hope this helps", "let me know",
            "i have crafted", "i have written"
        ]):
            continue
        if "highlights amrit" in l_lower or "amrit's alignment" in l_lower or "this aligns" in l_lower:
            continue
        filtered_lines.append(l_strip)
        
    cleaned = " ".join(filtered_lines).strip()
    cleaned = cleaned.strip('"\' \t\r\n')
    cleaned = re.sub(r',?\s*including\s+(?:current|expected)\s+ctc\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\[(?:company(?:\s*name)?|insert\s+[^\]]+|placeholder|recipient(?:\s*name)?)\]', '', cleaned, flags=re.IGNORECASE)
    return cleaned


def clean_job_title(title):
    if not title:
        return "Full Stack Developer"
    title_clean = title.strip()
    title_clean = re.sub(r'\s*\([^)]*\)', '', title_clean).strip()
    title_clean = re.sub(r'\bDevelopers\b', 'Developer', title_clean, flags=re.IGNORECASE)
    title_clean = re.sub(r'\bEngineers\b', 'Engineer', title_clean, flags=re.IGNORECASE)
    title_clean = re.sub(r'\bArchitects\b', 'Architect', title_clean, flags=re.IGNORECASE)
    if not title_clean or len(title_clean) > 45 or "multiple" in title_clean.lower() or "position" in title_clean.lower():
        if "lead" in title_clean.lower():
            return "Lead Full Stack Developer"
        elif "backend" in title_clean.lower():
            return "Backend Java Developer"
        elif "frontend" in title_clean.lower():
            return "Frontend React Developer"
        else:
            return "Full Stack Developer"
    return title_clean


def load_bandit_stats(logs_dir=None):
    logs_dir = logs_dir or _get_logs_dir()
    path = os.path.join(logs_dir, 'bandit_stats.json')
    default_stats = {
        "last_imap_sync_at": "",
        "variants": {
            "variant_a": {"description": "Executive Narrative + Quick Profile Snapshot", "sends": 0, "replies": 0},
            "variant_b": {"description": "Engineering Metrics & Architecture Impact First", "sends": 0, "replies": 0},
            "variant_c": {"description": "Ultra-Concise Direct Recruiter Note (<90 words)", "sends": 0, "replies": 0},
        }
    }
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict) and "variants" in data:
                    return data
        except Exception as e:
            logging.warning(f"Could not read bandit_stats.json: {e}")
    return default_stats


def save_bandit_stats(stats, logs_dir=None):
    logs_dir = logs_dir or _get_logs_dir()
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, 'bandit_stats.json')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"Could not write bandit_stats.json: {e}")


def select_email_variant(config, logs_dir=None):
    """Select an email template variant using Thompson Sampling (Multi-Armed Bandit)."""
    if not config.get('self_learning', {}).get('ab_testing_enabled', True):
        return 'variant_a'
    stats = load_bandit_stats(logs_dir)
    variants = stats.get('variants', {})
    if not variants:
        return 'variant_a'

    # Initial uniform exploration until each variant has at least 3 sends
    under_explored = [vid for vid, v in variants.items() if int(v.get('sends', 0)) < 3]
    if under_explored:
        under_explored.sort(key=lambda vid: int(variants[vid].get('sends', 0)))
        return under_explored[0]

    best_vid = 'variant_a'
    best_sample = -1.0
    for vid, v in variants.items():
        sends = max(0, int(v.get('sends', 0)))
        replies = max(0, min(sends, int(v.get('replies', 0))))
        alpha = replies + 1.0
        beta = (sends - replies) + 1.0
        sample = random.betavariate(alpha, beta)
        if sample > best_sample:
            best_sample = sample
            best_vid = vid
    return best_vid


def record_bandit_send(variant_id, logs_dir=None):
    stats = load_bandit_stats(logs_dir)
    variants = stats.setdefault('variants', {})
    if variant_id in variants:
        variants[variant_id]['sends'] = int(variants[variant_id].get('sends', 0)) + 1
        save_bandit_stats(stats, logs_dir)


def resolve_subject_placeholders(hint, profile, display_title, config=None):
    """
    Substitutes recruiter subject line template placeholders (e.g. [Total Exp], [Relevant Exp],
    [Current Location], [Notice Period], [Candidate Name], [Role]) with real profile details.
    
    Returns the resolved, validated subject string, or None if the template contains
    unresolved placeholders, leaked placeholder keywords, or invalid prose.
    """
    if not hint or not isinstance(hint, str):
        return None
    
    cleaned = hint.strip()
    # Strip common leading labels like "Subject:", "Subject Line:", "Sub:", etc.
    cleaned = re.sub(r'^(?:subject(?:\s*line)?|sub)\s*[:\-–—]\s*', '', cleaned, flags=re.IGNORECASE).strip()
    cleaned = cleaned.strip('"\'`')
    
    if not cleaned:
        return None

    # Filter out sentence-like text or generic instructions that aren't real subject templates
    cleaned_lower = cleaned.lower()
    if any(cleaned_lower.startswith(p) for p in [
        'we are ', 'we are looking', 'please send', 'please share', 'looking for ',
        'i am ', 'hiring for ', 'urgent requirement for', 'job description'
    ]):
        logging.info(f"[Subject Guard] Discarding prose sentence subject hint: '{cleaned}'")
        return None
    
    if cleaned.endswith(('.', '!', '?')) and not any(k in cleaned_lower for k in ['application', 'developer', 'engineer', 'architect', 'role', 'position']):
        logging.info(f"[Subject Guard] Discarding conversational subject hint: '{cleaned}'")
        return None

    sender_name = profile.get('full_name', 'Amrit Kumar')
    total_exp = profile.get('total_experience') or profile.get('total_exp') or '3.5 Years'
    rel_exp = profile.get('relevant_experience') or profile.get('relevant_exp') or '3+ Years'
    current_loc = profile.get('current_location') or profile.get('location') or 'Bengaluru, India'
    notice = profile.get('notice_period', '7 Days')
    current_comp = profile.get('current_company', 'Tech Solutions Inc.')
    phone = profile.get('phone', '+91-9876543210')
    email_addr = profile.get('email', 'candidate@example.com')

    naukri_qa = (config.get('naukri', {}).get('qa_bank', {}) if config else {})
    current_ctc = naukri_qa.get('current_ctc_lpa', '12') + " LPA"
    expected_ctc = naukri_qa.get('expected_ctc_lpa', '15') + " LPA"

    # Replacement mapping for bracketed placeholders: [...], <...>, {...}, or (...)
    replacements = [
        # Total Experience
        (r'[{\[<\(]\s*(?:total\s*(?:exp(?:erience)?|yoe|years(?:\s*of\s*experience)?)|overall\s*exp(?:erience)?)\s*[}\]>\)]', total_exp),
        # Relevant Experience
        (r'[{\[<\(]\s*(?:rel(?:evant)?\s*(?:exp(?:erience)?|yoe|years(?:\s*of\s*experience)?))\s*[}\]>\)]', rel_exp),
        # Generic Experience (only if not preceded by total/relevant)
        (r'[{\[<\(]\s*(?:exp(?:erience)?|yoe|years(?:\s*of\s*experience)?)\s*[}\]>\)]', total_exp),
        # Location / City
        (r'[{\[<\(]\s*(?:current\s*(?:location|city)|base\s*location|location|city|preferred\s*location)\s*[}\]>\)]', current_loc),
        # Notice Period
        (r'[{\[<\(]\s*(?:notice\s*(?:period(?:\s*days)?)?|np|availability|serving\s*notice)\s*[}\]>\)]', notice),
        # Candidate Name
        (r'[{\[<\(]\s*(?:candidate\s*name|your\s*name|applicant\s*name|full\s*name|name)\s*[}\]>\)]', sender_name),
        # Job Title / Role
        (r'[{\[<\(]\s*(?:job\s*(?:title|role|position)|role|position|designation|applied\s*role)\s*[}\]>\)]', display_title),
        # Current Company
        (r'[{\[<\(]\s*(?:current\s*(?:company|org(?:anization)?)|company|org(?:anization)?)\s*[}\]>\)]', current_comp),
        # CTC
        (r'[{\[<\(]\s*(?:current\s*ctc|ctc|current\s*salary)\s*[}\]>\)]', current_ctc),
        (r'[{\[<\(]\s*(?:expected\s*ctc|ectc|expected\s*salary)\s*[}\]>\)]', expected_ctc),
        # Contact / Phone / Email
        (r'[{\[<\(]\s*(?:phone|mobile|contact(?:\s*no|\s*number)?)\s*[}\]>\)]', phone),
        (r'[{\[<\(]\s*(?:email(?:\s*id|\s*address)?)\s*[}\]>\)]', email_addr),
    ]

    for pattern, val in replacements:
        cleaned = re.sub(pattern, val, cleaned, flags=re.IGNORECASE)

    # --- FAIL-SAFE GATE (Zero tolerance for unresolved placeholders) ---
    # 1. Any remaining unreplaced bracketed tokens: [...], <...>, {...}
    remaining_brackets = re.findall(r'\[[^\]]+\]|<[^>]+>|\{[^\}]+\}', cleaned)
    if remaining_brackets:
        logging.warning(
            f"[Subject Guard] Discarding subject hint with unresolved placeholder(s): {remaining_brackets} in '{hint}'"
        )
        return None

    # 2. Any lingering placeholder keywords
    leak_keywords = [
        r'\btotal\s*exp(?:erience)?\b',
        r'\brelevant\s*exp(?:erience)?\b',
        r'\bcurrent\s*location\b',
        r'\bnotice\s*period\b',
        r'\byears\s*of\s*experience\b',
        r'\bcandidate\s*name\b',
        r'\byour\s*name\b',
        r'\binsert\b',
        r'\bplaceholder\b',
        r'\byour\s*city\b',
    ]
    for lk in leak_keywords:
        if re.search(lk, cleaned, re.IGNORECASE):
            logging.warning(
                f"[Subject Guard] Discarding subject hint containing placeholder keyword '{lk}': '{cleaned}'"
            )
            return None

    # 3. Length sanity check
    if len(cleaned) < 8 or len(cleaned) > 100:
        logging.warning(f"[Subject Guard] Discarding subject hint with invalid length ({len(cleaned)}): '{cleaned}'")
        return None

    return cleaned


def compose_email_subject(profile, jd_info, display_title, config=None):
    sender_name = profile.get('full_name', 'Amrit Kumar')
    notice = profile.get('notice_period', '7 Days')
    default_subject = f"Application: {display_title} | {sender_name} (3+ YOE | Immediate/{notice} Joiner)"
    
    hint = (jd_info.get('subject_line_hint') or '').strip()
    if not hint:
        return default_subject

    if config is None:
        try:
            config = load_config()
        except Exception:
            config = {}

    resolved_subject = resolve_subject_placeholders(hint, profile, display_title, config=config)
    if resolved_subject:
        return resolved_subject
    
    return default_subject


def compose_formal_email_body(profile, company_name, job_title, alignment_para, jd_info=None, variant_id="variant_a"):
    clean_alignment = clean_llm_text(alignment_para)
    raw_company = (company_name or '').strip()
    has_company = not is_unspecified_company(raw_company)
    jd_info = jd_info or {}
    recruiter_name = (jd_info.get('recruiter_name') or '').strip()

    if recruiter_name:
        greeting = f"Hi {recruiter_name},"
    elif has_company:
        greeting = f"Dear Hiring Team at {raw_company},"
    else:
        greeting = "Dear Hiring Team,"

    closing_org = f"{raw_company}'s engineering goals" if has_company else "your team's engineering goals"
    reqs = jd_info.get('key_requirements') or ["Java 21", "Spring Boot 3", "Microservices", "React 19", "AWS"]
    skills_line = ", ".join(reqs[:4])
    notice = profile.get('notice_period', '7 days')
    phone = profile.get('phone', '+91-9876543210')
    email_addr = profile.get('email', 'candidate@example.com')
    linkedin = profile.get('linkedin', 'https://linkedin.com/in/yourprofile')
    github = profile.get('github', 'https://github.com/yourusername')
    full_name = profile.get('full_name', 'Amrit Kumar')

    if variant_id == "variant_b":
        body = f"""{greeting}

I am applying for the {job_title} position. Over the past 3+ years as a Full Stack Developer at Tech Solutions Inc. and Global Software Systems, I have delivered production platforms with measurable engineering impact:
• Reduced API response times by 35% and PostgreSQL query latency by 40% across millions of records.
• Architected Spring Boot 3 microservices, React 19 frontends, Python FastAPI pipelines, and AWS (EC2, S3, RDS) deployments.
• Core Match for Role: {skills_line} | Experience: 3+ Years | Notice Period: {notice} (Immediate Joiner).

{clean_alignment}

Please find my tailored resume and cover letter attached. I would welcome a quick call to discuss how I can contribute to {closing_org}.

Best regards,

{full_name}
Full Stack Developer
Mobile: {phone}
Email: {email_addr}
LinkedIn: {linkedin}
GitHub: {github}"""
    elif variant_id == "variant_c":
        body = f"""{greeting}

I am reaching out regarding the {job_title} opening. {clean_alignment}

Quick Profile Snapshot:
• Experience: 3+ Years (Java 21, Spring Boot 3, Microservices, React 19, PostgreSQL, AWS)
• Key Focus: {skills_line}
• Availability: Immediate / {notice} Notice Period

My tailored resume and cover letter are attached for your consideration. Looking forward to connecting.

Best regards,

{full_name}
Mobile: {phone} | Email: {email_addr}
LinkedIn: {linkedin} | GitHub: {github}"""
    else:
        body = f"""{greeting}

I am writing to express my strong interest in the {job_title} position{" at " + raw_company if has_company else ""}.

{clean_alignment}

Quick Profile Snapshot:
• Total Experience: 3+ Years (Full Stack & Backend Engineering)
• Core Stack: {skills_line}, Java 21, Spring Boot 3, React 19, PostgreSQL, AWS
• Key Impact: 35% faster server response times & 40% lower DB query latency
• Notice Period: {notice} (Immediate Joiner)

I have attached both my tailored resume and cover letter for your review. Thank you for your time and consideration, and I look forward to discussing how my technical background aligns with {closing_org}.

Sincerely,

{full_name}
Full Stack Developer
Mobile: {phone}
Email: {email_addr}
LinkedIn: {linkedin}
GitHub: {github}"""
    return body


def compose_html_email_body(plain_body, profile):
    """Convert the plain-text email body into a clean, responsive HTML email with clickable links and bolded labels."""
    escaped = (
        plain_body.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )
    # Bold bullet labels like "• Total Experience:"
    escaped = re.sub(r'•\s*([^:\n]+):', r'&bull; <strong>\1:</strong>', escaped)
    escaped = escaped.replace('Quick Profile Snapshot:', '<strong>Quick Profile Snapshot:</strong>')

    linkedin = profile.get('linkedin', 'https://linkedin.com/in/yourprofile')
    github = profile.get('github', 'https://github.com/yourusername')
    email_addr = profile.get('email', 'candidate@example.com')

    if linkedin:
        escaped = escaped.replace(linkedin, f'<a href="{linkedin}" style="color:#1A2B4C;text-decoration:none;">{linkedin}</a>')
    if github:
        escaped = escaped.replace(github, f'<a href="{github}" style="color:#1A2B4C;text-decoration:none;">{github}</a>')
    if email_addr:
        escaped = escaped.replace(email_addr, f'<a href="mailto:{email_addr}" style="color:#1A2B4C;text-decoration:none;">{email_addr}</a>')

    paragraphs = [p.strip() for p in escaped.split('\n\n') if p.strip()]
    html_paras = []
    for p in paragraphs:
        p_html = p.replace('\n', '<br/>')
        html_paras.append(f'<p style="margin:0 0 12px 0;line-height:1.55;">{p_html}</p>')

    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'font-size:14px;color:#1F2937;max-width:640px;">'
        + "".join(html_paras)
        + '</div>'
    )


def dispatch_gmail_smtp(
    config,
    recipient_email,
    company_name,
    job_title,
    formal_email_body,
    pdf_resume_path,
    pdf_cover_path=None,
    jd_info=None,
    html_email_body=None
):
    try:
        fresh_config = load_config()
        if fresh_config:
            config = fresh_config
    except Exception:
        pass
    email_cfg = config.get('email', {})
    sender_email = email_cfg.get('sender_email', '')
    app_password = email_cfg.get('sender_app_password', '')
    sender_name = email_cfg.get('sender_name', 'Amrit Kumar')
    profile = config.get('profile', {})
    
    display_company = company_name if not is_unspecified_company(company_name) else "Hiring Organization"
    if not app_password or app_password == "YOUR_GMAIL_APP_PASSWORD":
        logging.warning("Gmail App Password not configured in config.json. Skipping SMTP email dispatch.")
        logging.info(f"Generated PDFs are ready in output/ folder for {display_company}.")
        return False

    display_title = clean_job_title(job_title)
    logging.info(f"Dispatching email via Gmail SMTP to {recipient_email} for {display_title} at {display_company}...")
    
    msg = MIMEMultipart('mixed')
    msg['From'] = f"{sender_name} <{sender_email}>"
    msg['To'] = recipient_email
    msg['Subject'] = compose_email_subject(profile, jd_info or {}, display_title, config=config)
    
    alt_part = MIMEMultipart('alternative')
    alt_part.attach(MIMEText(formal_email_body, 'plain', 'utf-8'))
    if html_email_body:
        alt_part.attach(MIMEText(html_email_body, 'html', 'utf-8'))
    msg.attach(alt_part)
    
    # Attach Tailored Resume PDF
    if pdf_resume_path and os.path.exists(pdf_resume_path):
        with open(pdf_resume_path, 'rb') as f:
            attach = MIMEApplication(f.read(), _subtype="pdf")
            attach.add_header('Content-Disposition', 'attachment', filename=os.path.basename(pdf_resume_path))
            msg.attach(attach)

    # Attach Tailored Cover Letter PDF
    if pdf_cover_path and os.path.exists(pdf_cover_path):
        with open(pdf_cover_path, 'rb') as f:
            attach = MIMEApplication(f.read(), _subtype="pdf")
            attach.add_header('Content-Disposition', 'attachment', filename=os.path.basename(pdf_cover_path))
            msg.attach(attach)
            
    smtp_server = email_cfg.get('smtp_server', 'smtp.gmail.com')
    smtp_port = email_cfg.get('smtp_port', 587)
    for attempt in range(1, 4):
        try:
            if attempt % 2 == 1:
                with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(sender_email, app_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP_SSL(smtp_server, 465, timeout=30) as server:
                    server.ehlo()
                    server.login(sender_email, app_password)
                    server.send_message(msg)

            logging.info(f"Successfully sent application email (Resume & Cover Letter attached) to {recipient_email}")
            return True
        except Exception as e:
            if attempt < 3:
                logging.warning(f"Transient SMTP error on attempt {attempt}/3 ({e}). Retrying in {attempt * 3}s...")
                time.sleep(attempt * 3)
            else:
                logging.error(f"Failed to send email via SMTP after 3 attempts: {e}")
                return False


def normalize_email_identity(email):
    """
    Canonicalize an email address so case variations, '+' aliases, and
    Gmail/OCR dot variations ('sadhana.parmar@...' vs 'sadhanaparmar@...')
    resolve to the same recipient identity.
    """
    if not email or '@' not in str(email):
        return ""
    cleaned = str(email).strip().strip('.,;:()[]{}<>"\'').lower()
    if '@' not in cleaned:
        return ""
    local_part, domain = cleaned.split('@', 1)
    if domain == 'googlemail.com':
        domain = 'gmail.com'
    local_part = local_part.split('+', 1)[0]
    local_part = local_part.replace('.', '')
    return f"{local_part}@{domain}"


def load_suppression_list(logs_dir, config):
    """Load global suppression list (unsubscribes, bounces, manual blocklist) from JSON + config."""
    supp_path = os.path.join(logs_dir, "suppression_list.json")
    supp_data = {"suppressed_emails": {}, "suppressed_domains": {}}
    if os.path.exists(supp_path):
        try:
            with open(supp_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    supp_data["suppressed_emails"] = {
                        normalize_email_identity(k): v
                        for k, v in loaded.get("suppressed_emails", {}).items()
                        if normalize_email_identity(k)
                    }
                    supp_data["suppressed_domains"] = {
                        k.strip().lower().lstrip('@'): v
                        for k, v in loaded.get("suppressed_domains", {}).items()
                        if k.strip()
                    }
        except Exception as e:
            logging.warning(f"Could not read suppression_list.json: {e}")

    dedup_cfg = config.get("deduplication", {})
    updated = False
    for em in dedup_cfg.get("suppressed_emails", []):
        norm = normalize_email_identity(em)
        if norm and norm not in supp_data["suppressed_emails"]:
            supp_data["suppressed_emails"][norm] = {
                "raw_email": em,
                "reason": "CONFIG_BLOCKLIST",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            updated = True

    for dom in dedup_cfg.get("suppressed_domains", []):
        clean_dom = str(dom).strip().lower().lstrip('@')
        if clean_dom and clean_dom not in supp_data["suppressed_domains"]:
            supp_data["suppressed_domains"][clean_dom] = {
                "reason": "CONFIG_DOMAIN_BLOCKLIST",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            updated = True

    if updated or not os.path.exists(supp_path):
        try:
            with open(supp_path, 'w', encoding='utf-8') as f:
                json.dump(supp_data, f, indent=2, sort_keys=True)
        except Exception as e:
            logging.warning(f"Could not write suppression_list.json: {e}")

    return supp_data


def _merge_sent_record(sent_records, norm_key, raw_email, record_data):
    existing = sent_records.get(norm_key)
    if not existing:
        entry = dict(record_data)
        entry.setdefault("raw_email", raw_email)
        entry.setdefault("normalized_email", norm_key)
        entry.setdefault("status", "SENT")
        entry.setdefault("send_count", 1)
        sent_records[norm_key] = entry
        return True

    updated = False
    new_ts = record_data.get("sent_at", "")
    old_ts = existing.get("sent_at", "")
    if new_ts and new_ts > old_ts:
        existing["sent_at"] = new_ts
        if record_data.get("company"):
            existing["company"] = record_data["company"]
        if record_data.get("job_title"):
            existing["job_title"] = record_data["job_title"]
        updated = True
    if "normalized_email" not in existing:
        existing["normalized_email"] = norm_key
        updated = True
    if "raw_email" not in existing and raw_email:
        existing["raw_email"] = raw_email
        updated = True
    if "status" not in existing:
        existing["status"] = "SENT"
        updated = True
    return updated


def load_sent_emails(logs_dir, sender_email=""):
    sent_path = os.path.join(logs_dir, "sent_emails.json")
    sent_records = {}
    needs_save = False
    norm_sender = normalize_email_identity(sender_email) if sender_email else ""

    if os.path.exists(sent_path):
        try:
            with open(sent_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        norm_k = normalize_email_identity(k)
                        if not norm_k or (norm_sender and norm_k == norm_sender):
                            continue
                        if norm_k != k:
                            needs_save = True
                        raw_em = v.get("raw_email", k) if isinstance(v, dict) else k
                        rec = v if isinstance(v, dict) else {"sent_at": str(v)}
                        if _merge_sent_record(sent_records, norm_k, raw_em, rec):
                            needs_save = True
        except Exception as e:
            logging.warning(f"Could not read sent_emails.json: {e}")

    log_file = os.path.join(logs_dir, "automation.log")
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if "Successfully sent application email" in line:
                        m = re.search(r'to\s+([\w\.\+\-]+@[\w\.\-]+\.[a-zA-Z]{2,})', line)
                        if m:
                            raw_addr = m.group(1).strip()
                            norm_addr = normalize_email_identity(raw_addr)
                            if not norm_addr or (norm_sender and norm_addr == norm_sender):
                                continue
                            ts = line[:19] if len(line) >= 19 else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            if norm_addr not in sent_records:
                                _merge_sent_record(
                                    sent_records,
                                    norm_addr,
                                    raw_addr,
                                    {"sent_at": ts, "source": "automation.log", "status": "SENT", "send_count": 1}
                                )
                                needs_save = True
        except Exception as e:
            logging.warning(f"Could not scan automation.log for sent emails: {e}")

    if needs_save or not os.path.exists(sent_path):
        try:
            with open(sent_path, 'w', encoding='utf-8') as f:
                json.dump(sent_records, f, indent=2, sort_keys=True)
        except Exception as e:
            logging.warning(f"Could not write sent_emails.json: {e}")

    return sent_records


def _parse_timestamp(ts_str):
    if not ts_str:
        return None
    clean_ts = str(ts_str).strip()[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean_ts, fmt)
        except ValueError:
            continue
    return None


def can_send_email(recipient_email, config, sent_records, suppression_data, sender_email=""):
    """
    Multi-tier pre-send guard:
      - Self-test bypass (sender_email)
      - Tier 1: Global suppression list (email or domain)
      - Tier 2 & 3: Cooldown window / duplicate check on normalized identity
      - Tier 4: Per-domain daily throttle for corporate domains
    Returns (allowed: bool, status_code: str, message: str)
    """
    dedup_cfg = config.get("deduplication", {})
    if not dedup_cfg.get("enabled", True):
        return True, "ALLOWED_DEDUP_DISABLED", "Deduplication disabled in config"

    norm_email = normalize_email_identity(recipient_email)
    if not norm_email:
        return False, "SKIPPED_INVALID_EMAIL", f"Invalid email format: {recipient_email}"

    norm_sender = normalize_email_identity(sender_email) if sender_email else ""
    if norm_sender and norm_email == norm_sender:
        return True, "ALLOWED_SELF_TEST", "Self-test email bypass"

    domain = norm_email.split('@', 1)[1]

    # Tier 1: Global Suppression List
    supp_emails = (suppression_data or {}).get("suppressed_emails", {})
    supp_domains = (suppression_data or {}).get("suppressed_domains", {})
    if norm_email in supp_emails:
        reason = supp_emails[norm_email].get("reason", "MANUAL")
        return False, "SKIPPED_GLOBAL_SUPPRESSION", f"{recipient_email} is in global suppression_list ({reason})"
    if domain in supp_domains:
        reason = supp_domains[domain].get("reason", "DOMAIN_BLOCKLIST")
        return False, "SKIPPED_GLOBAL_SUPPRESSION", f"Domain @{domain} is in global suppression_list ({reason})"

    # Tier 2 & 3: Duplicate & Cooldown Check
    cooldown_days = dedup_cfg.get("cooldown_days", 14)
    if norm_email in sent_records:
        prev = sent_records[norm_email]
        prev_sent_at = prev.get("sent_at", "")
        prev_raw = prev.get("raw_email", recipient_email)
        if cooldown_days is None or cooldown_days <= 0:
            return (
                False,
                "SKIPPED_DUPLICATE_PERMANENT",
                f"Already sent to {prev_raw} on {prev_sent_at} (permanent deduplication)"
            )
        sent_dt = _parse_timestamp(prev_sent_at)
        if sent_dt:
            elapsed_days = (datetime.now() - sent_dt).total_seconds() / 86400.0
            if elapsed_days < float(cooldown_days):
                rem_days = max(1, int(round(float(cooldown_days) - elapsed_days)))
                return (
                    False,
                    "SKIPPED_COOLDOWN_ACTIVE",
                    f"Already sent to {prev_raw} on {prev_sent_at} ({elapsed_days:.1f}d ago; {cooldown_days}d cooldown active, ~{rem_days}d remaining)"
                )

    # Tier 4: Corporate Domain Daily Throttle
    max_per_domain = dedup_cfg.get("max_per_domain_per_day", 5)
    if max_per_domain and max_per_domain > 0 and domain not in GENERIC_EMAIL_DOMAINS:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        domain_sends_today = 0
        for k, v in sent_records.items():
            if k.endswith(f"@{domain}") and str(v.get("sent_at", "")).startswith(today_prefix):
                domain_sends_today += 1
        if domain_sends_today >= int(max_per_domain):
            return (
                False,
                "SKIPPED_DOMAIN_THROTTLE",
                f"Corporate domain @{domain} reached daily limit ({domain_sends_today}/{max_per_domain} sent today)"
            )

    return True, "ALLOWED", "Clear to send"


def record_sent_email(
    logs_dir,
    recipient_email,
    company_name,
    job_title,
    sent_records,
    email_variant="variant_a",
    key_requirements=None,
    resume_summary="",
    alignment_paragraph=""
):
    sent_path = os.path.join(logs_dir, "sent_emails.json")
    norm_key = normalize_email_identity(recipient_email)
    if not norm_key:
        return
    prev = sent_records.get(norm_key, {})
    prev_count = int(prev.get("send_count", 0)) if isinstance(prev, dict) else 0
    sent_records[norm_key] = {
        "normalized_email": norm_key,
        "raw_email": recipient_email.strip(),
        "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "company": company_name or prev.get("company", ""),
        "job_title": job_title or prev.get("job_title", ""),
        "status": "SENT",
        "send_count": prev_count + 1,
        "email_variant": email_variant,
        "key_requirements": key_requirements or prev.get("key_requirements", []),
        "resume_summary": resume_summary or prev.get("resume_summary", ""),
        "alignment_paragraph": alignment_paragraph or prev.get("alignment_paragraph", "")
    }
    try:
        with open(sent_path, 'w', encoding='utf-8') as f:
            json.dump(sent_records, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"Could not update sent_emails.json: {e}")

    try:
        import crm_tracker
        crm_tracker.record_outreach(
            email=recipient_email.strip(),
            company=company_name or prev.get("company", ""),
            role=job_title or prev.get("job_title", ""),
            source_channel="COLD_EMAIL",
            metadata={"email_variant": email_variant}
        )
    except Exception:
        pass


def record_skipped_email(logs_dir, recipient_email, skip_code, sent_records):
    sent_path = os.path.join(logs_dir, "sent_emails.json")
    norm_key = normalize_email_identity(recipient_email)
    if not norm_key or norm_key not in sent_records:
        return
    entry = sent_records[norm_key]
    entry["last_skipped_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry["last_skip_reason"] = skip_code
    entry["skip_count"] = int(entry.get("skip_count", 0)) + 1
    try:
        with open(sent_path, 'w', encoding='utf-8') as f:
            json.dump(sent_records, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"Could not update skip metadata in sent_emails.json: {e}")


def promote_to_winning_examples(logs_dir, norm_email, record):
    """Promote an application that received a recruiter reply into winning_examples.json for future few-shot learning."""
    summary = (record.get("resume_summary") or "").strip()
    alignment = (record.get("alignment_paragraph") or "").strip()
    if len(summary) < 40 and len(alignment) < 40:
        return
    path = os.path.join(logs_dir, "winning_examples.json")
    examples = load_winning_examples(logs_dir)
    rec_id = f"replied_{norm_email}"
    if any(ex.get("id") == rec_id for ex in examples):
        return
    examples.append({
        "id": rec_id,
        "job_title": record.get("job_title", "Full Stack Developer"),
        "key_requirements": record.get("key_requirements") or ["Java 21", "Spring Boot 3", "React 19", "AWS"],
        "resume_summary": summary,
        "alignment_paragraph": alignment,
        "source": record.get("raw_email", norm_email),
        "outcome": "REPLIED",
        "replied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(examples, f, indent=2)
        logging.info(f"[Self-Learning] Promoted {record.get('raw_email', norm_email)} application to winning_examples.json!")
    except Exception as e:
        logging.warning(f"Could not update winning_examples.json: {e}")


def sync_imap_feedback(config, logs_dir, sent_records, suppression_data):
    """
    Phase 3 Closed-Loop Self-Training:
    Periodically scans Gmail INBOX via read-only IMAP to detect:
      1. Hard bounces (mailer-daemon) -> auto-adds to suppression_list.json
      2. Recruiter replies -> marks outcome='REPLIED', updates bandit_stats.json, and promotes to winning_examples.json
    """
    sl_cfg = config.get("self_learning", {})
    if not sl_cfg.get("enabled", True) or not sl_cfg.get("imap_feedback_sync", True):
        return

    email_cfg = config.get("email", {})
    sender_email = email_cfg.get("sender_email", "")
    app_password = email_cfg.get("sender_app_password", "")
    if not sender_email or not app_password or app_password == "YOUR_GMAIL_APP_PASSWORD":
        return

    stats = load_bandit_stats(logs_dir)
    last_sync_dt = _parse_timestamp(stats.get("last_imap_sync_at", ""))
    interval_hours = float(sl_cfg.get("imap_sync_interval_hours", 6))
    if last_sync_dt and (datetime.now() - last_sync_dt).total_seconds() < interval_hours * 3600:
        return

    imap_server = sl_cfg.get("imap_server", "imap.gmail.com")
    since_date = (datetime.now() - timedelta(days=14)).strftime("%d-%b-%Y")
    logging.info(f"[Self-Learning] Running periodic Gmail IMAP feedback sync (since {since_date})...")

    sent_updated = False
    supp_updated = False
    try:
        mail = imaplib.IMAP4_SSL(imap_server, timeout=15)
        mail.login(sender_email, app_password)
        mail.select("INBOX", readonly=True)

        # 1. Detect Hard Bounces from mailer-daemon
        typ, bounce_ids = mail.search(None, f'(FROM "mailer-daemon" SINCE "{since_date}")')
        if typ == "OK" and bounce_ids and bounce_ids[0]:
            for msg_id in bounce_ids[0].split()[-30:]:
                _, msg_data = mail.fetch(msg_id, "(BODY.PEEK[TEXT])")
                if not msg_data or not msg_data[0]:
                    continue
                raw_body = msg_data[0][1].decode("utf-8", errors="ignore")
                found_addrs = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', raw_body)
                for addr in found_addrs:
                    norm_b = normalize_email_identity(addr)
                    if norm_b in sent_records and norm_b not in suppression_data["suppressed_emails"]:
                        suppression_data["suppressed_emails"][norm_b] = {
                            "raw_email": addr,
                            "reason": "HARD_BOUNCE_IMAP",
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        sent_records[norm_b]["status"] = "BOUNCED"
                        supp_updated = True
                        sent_updated = True
                        logging.info(f"[Self-Learning] Auto-suppressed bounced address: {addr}")

        # 2. Detect Recruiter Replies from known recipients in sent_records
        typ, recent_ids = mail.search(None, f'(SINCE "{since_date}")')
        if typ == "OK" and recent_ids and recent_ids[0]:
            for msg_id in recent_ids[0].split()[-100:]:
                _, hdr_data = mail.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if not hdr_data or not hdr_data[0]:
                    continue
                hdr_text = hdr_data[0][1].decode("utf-8", errors="ignore")
                from_addrs = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', hdr_text)
                for f_addr in from_addrs:
                    norm_f = normalize_email_identity(f_addr)
                    if norm_f in sent_records and sent_records[norm_f].get("outcome") != "REPLIED":
                        rec = sent_records[norm_f]
                        rec["outcome"] = "REPLIED"
                        rec["replied_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        variant_used = rec.get("email_variant", "variant_a")
                        if variant_used in stats.get("variants", {}):
                            stats["variants"][variant_used]["replies"] = int(stats["variants"][variant_used].get("replies", 0)) + 1
                        promote_to_winning_examples(logs_dir, norm_f, rec)
                        sent_updated = True
                        logging.info(f"[Self-Learning] Detected recruiter reply from {f_addr} (variant={variant_used})!")

                        # Close the loop: if lead came from LinkedIn Recruiter Harvester, credit query stream
                        try:
                            li_ledger_path = os.path.join(logs_dir, "linkedin_applied_jobs.json")
                            if os.path.exists(li_ledger_path):
                                with open(li_ledger_path, "r", encoding="utf-8") as lif:
                                    li_data = json.load(lif)
                                h_posts = li_data.get("harvested_posts", {})
                                post_lead = h_posts.get(norm_f) or h_posts.get(rec.get("raw_email", "").lower())
                                if post_lead and post_lead.get("query"):
                                    src_query = post_lead["query"]
                                    qstats_path = os.path.join(logs_dir, "linkedin_query_stats.json")
                                    if os.path.exists(qstats_path):
                                        with open(qstats_path, "r", encoding="utf-8") as qsf:
                                            q_stats = json.load(qsf)
                                        if src_query in q_stats.get("queries", {}):
                                            q_stats["queries"][src_query]["replies"] = int(q_stats["queries"][src_query].get("replies", 0)) + 1
                                            with open(qstats_path, "w", encoding="utf-8") as qsf:
                                                json.dump(q_stats, qsf, indent=2, sort_keys=True)
                                            logging.info(f"[Self-Learning] Credited recruiter reply to LinkedIn query stream: '{src_query}'")
                        except Exception as e:
                            logging.debug(f"[Self-Learning] Could not credit LinkedIn query stats: {e}")

        mail.logout()
    except Exception as e:
        logging.warning(f"[Self-Learning] IMAP feedback sync skipped/warning: {e}")

    stats["last_imap_sync_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_bandit_stats(stats, logs_dir)

    if supp_updated:
        supp_path = os.path.join(logs_dir, "suppression_list.json")
        try:
            with open(supp_path, 'w', encoding='utf-8') as f:
                json.dump(suppression_data, f, indent=2, sort_keys=True)
        except Exception:
            pass

    if sent_updated:
        sent_path = os.path.join(logs_dir, "sent_emails.json")
        try:
            with open(sent_path, 'w', encoding='utf-8') as f:
                json.dump(sent_records, f, indent=2, sort_keys=True)
        except Exception:
            pass


def get_dropzone_path(config):
    paths = config.get('paths', {})
    configured = paths.get('dropzone')
    if configured:
        expanded = os.path.expanduser(configured)
        if os.path.exists(expanded):
            return expanded
        parent = os.path.dirname(expanded)
        if os.path.exists(parent):
            os.makedirs(expanded, exist_ok=True)
            return expanded

    home = os.path.expanduser("~")
    email = config.get('email', {}).get('sender_email', '')
    if email:
        gdrive_candidate = f"{home}/Library/CloudStorage/GoogleDrive-{email}/My Drive/Job_Dropzone"
        if os.path.exists(gdrive_candidate):
            return gdrive_candidate
        gdrive_parent = f"{home}/Library/CloudStorage/GoogleDrive-{email}/My Drive"
        if os.path.exists(gdrive_parent):
            os.makedirs(gdrive_candidate, exist_ok=True)
            return gdrive_candidate

    matches = glob.glob(f"{home}/Library/CloudStorage/GoogleDrive-*/My Drive")
    if matches:
        target = os.path.join(matches[0], "Job_Dropzone")
        os.makedirs(target, exist_ok=True)
        return target

    local_dropzone = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dropzone')
    os.makedirs(local_dropzone, exist_ok=True)
    return local_dropzone


def maybe_run_daily_naukri_automation(config, logs_dir, force_naukri=False):
    """
    Trigger the daily Naukri Profile Refresh + Auto-Applier once per day (starting at 9:00 AM IST)
    once the persistent Chrome profile has been initialized via 'naukri_agent.py --mode login'.
    """
    naukri_cfg = config.get('naukri', {})
    if not naukri_cfg.get('enabled', True) and not force_naukri:
        return

    profile_dir = os.path.expanduser(
        naukri_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/naukri')
    )
    if not force_naukri and not os.path.exists(profile_dir):
        return

    ledger_path = os.path.join(logs_dir, 'naukri_applied_jobs.json')
    today_str = datetime.now().strftime("%Y-%m-%d")
    if not force_naukri and os.path.exists(ledger_path):
        try:
            with open(ledger_path, 'r', encoding='utf-8') as f:
                ledger_data = json.load(f)
                if ledger_data.get("last_daily_auto_run_date") == today_str:
                    return
        except Exception:
            pass

    try:
        import naukri_agent
        logging.info("[Naukri Daily Scheduler] Starting scheduled daily Naukri Profile Refresh & Auto-Applier...")
        naukri_agent.run_naukri_pipeline(mode="all", interactive_login=force_naukri)
    except Exception as e:
        logging.warning(f"[Naukri Daily Scheduler] Skipped or encountered error: {e}")


def maybe_run_daily_instahyre_automation(config, logs_dir, force_instahyre=False):
    """
    Trigger the daily Instahyre Profile Refresh + Auto-Applier once per day (starting at 9:00 AM IST)
    once the persistent Chrome profile has been initialized via 'instahyre_agent.py --mode login'.
    """
    insta_cfg = config.get('instahyre', {})
    if not insta_cfg.get('enabled', True) and not force_instahyre:
        return

    profile_dir = os.path.expanduser(
        insta_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/instahyre')
    )
    if not force_instahyre and not os.path.exists(profile_dir):
        return

    ledger_path = os.path.join(logs_dir, 'instahyre_applied_jobs.json')
    today_str = datetime.now().strftime("%Y-%m-%d")
    if not force_instahyre:
        if not os.path.exists(ledger_path):
            return
        try:
            with open(ledger_path, 'r', encoding='utf-8') as f:
                ledger_data = json.load(f)
                if not ledger_data.get("login_verified", False):
                    return
                if ledger_data.get("last_daily_auto_run_date") == today_str:
                    return
        except Exception:
            return

    try:
        import instahyre_agent
        logging.info("[Instahyre Daily Scheduler] Starting scheduled daily Instahyre Profile Refresh & Auto-Applier...")
        instahyre_agent.run_instahyre_pipeline(mode="all", interactive_login=force_instahyre)
    except Exception as e:
        logging.warning(f"[Instahyre Daily Scheduler] Skipped or encountered error: {e}")


def maybe_run_daily_linkedin_automation(config, logs_dir, force_linkedin=False):
    """
    Trigger the daily LinkedIn Recruiter Post Harvester + 1-Click Easy Apply once per day (starting at 9:00 AM IST)
    once the persistent Chrome profile has been initialized via 'linkedin_agent.py --mode login'.
    """
    li_cfg = config.get('linkedin', {})
    if not li_cfg.get('enabled', True) and not force_linkedin:
        return

    profile_dir = os.path.expanduser(
        li_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/linkedin')
    )
    if not force_linkedin and not os.path.exists(profile_dir):
        return

    ledger_path = os.path.join(logs_dir, 'linkedin_applied_jobs.json')
    today_str = datetime.now().strftime("%Y-%m-%d")
    if not force_linkedin:
        if not os.path.exists(ledger_path):
            return
        try:
            with open(ledger_path, 'r', encoding='utf-8') as f:
                ledger_data = json.load(f)
                if not ledger_data.get("login_verified", False):
                    return
                if ledger_data.get("last_daily_auto_run_date") == today_str:
                    return
        except Exception:
            return

    try:
        import linkedin_agent
        logging.info("[LinkedIn Daily Scheduler] Starting scheduled daily LinkedIn Recruiter Post Harvester & Easy Apply...")
        linkedin_agent.run_linkedin_pipeline(mode="all", interactive_login=force_linkedin)
    except Exception as e:
        logging.warning(f"[LinkedIn Daily Scheduler] Skipped or encountered error: {e}")


def process_dropzone():
    import fcntl
    lock_path = "/tmp/job_automation_agent.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, IOError):
        print("Another instance of Job Automation is currently executing. Skipping to prevent duplicate emails.")
        return

    config = load_config()
    paths = config.get('paths', {})
    dropzone_dir = get_dropzone_path(config)
    output_dir = os.path.expanduser(paths.get('output', '~/Documents/JobAutomation/output'))
    logs_dir = os.path.expanduser(paths.get('logs', '~/Documents/JobAutomation/logs'))
    
    setup_logging(logs_dir)
    os.makedirs(output_dir, exist_ok=True)
    
    now = datetime.now()
    current_hour = now.hour
    force_run = '--force' in sys.argv
    force_naukri = '--naukri' in sys.argv
    force_instahyre = '--instahyre' in sys.argv
    force_linkedin = '--linkedin' in sys.argv

    # Run daily 9:00 AM Naukri, Instahyre & LinkedIn Profile Refresh & Auto-Appliers (9 <= hour < 18)
    if force_naukri or (9 <= current_hour < 18):
        maybe_run_daily_naukri_automation(config, logs_dir, force_naukri=force_naukri)
    if force_instahyre or (9 <= current_hour < 18):
        maybe_run_daily_instahyre_automation(config, logs_dir, force_instahyre=force_instahyre)
    if force_linkedin or (9 <= current_hour < 18):
        maybe_run_daily_linkedin_automation(config, logs_dir, force_linkedin=force_linkedin)

    # Check 10:00 AM to 6:00 PM processing window (10 <= hour < 18, unless --force flag is passed)
    if not force_run and not (10 <= current_hour < 18):
        logging.info(f"Current time ({now.strftime('%H:%M')}) is outside the active dispatch window (10:00 AM - 6:00 PM). Dropzone items will be preserved and processed when the window opens at 10:00 AM.")
        return

    logging.info(f"Active Dropzone Directory: {dropzone_dir}")
    logging.info("Starting Job Automation Processing Run...")

    profile = config.get('profile', {})
    sender_email = config.get('email', {}).get('sender_email', '')
    suppression_data = load_suppression_list(logs_dir, config)
    sent_records = load_sent_emails(logs_dir, sender_email=sender_email)

    # Periodic Closed-Loop IMAP Feedback Sync (detects replies & hard bounces every 6 hours)
    sync_imap_feedback(config, logs_dir, sent_records, suppression_data)

    # Periodic Inbound Recruiter & Link Sweep
    try:
        import recruiter_copilot
        recruiter_copilot.run_recruiter_copilot_cycle()
    except Exception as e:
        logging.debug(f"[RecruiterCopilot] Periodic sweep notice: {e}")
    
    local_dropzone = os.path.expanduser('~/Documents/JobAutomation/dropzone')
    os.makedirs(local_dropzone, exist_ok=True)
    local_cold = os.path.join(local_dropzone, 'cold_emails')
    os.makedirs(local_cold, exist_ok=True)

    scan_dirs = [dropzone_dir]
    if os.path.abspath(local_dropzone) != os.path.abspath(dropzone_dir):
        scan_dirs.append(local_dropzone)
    gd_cold = os.path.join(dropzone_dir, 'Cold_Emails')
    if os.path.exists(gd_cold):
        scan_dirs.append(gd_cold)
    scan_dirs.append(local_cold)

    files = []
    for d_dir in scan_dirs:
        if os.path.exists(d_dir):
            files.extend([
                f for f in glob.glob(os.path.join(d_dir, '*'))
                if os.path.isfile(f) and not os.path.basename(f).startswith('.')
            ])
    files = sorted(files)

    if not files:
        logging.info("Dropzone is empty. No job posts to process.")
        return
        
    logging.info(f"Found {len(files)} file(s) in dropzone: {[os.path.basename(f) for f in files]}")
    
    for file_path in files:
        filename = os.path.basename(file_path)
        logging.info(f"--- Processing File: {filename} ---")
        
        # Autonomous Intent & Knowledge Ingestion from Screenshots
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.png', '.jpg', '.jpeg', '.webp', '.bmp']:
            try:
                intent_miner.ingest_screenshot_knowledge(file_path, config=config, logs_dir=logs_dir)
            except Exception as e:
                logging.debug(f"[IntentMiner] Non-fatal ingestion error for {filename}: {e}")
        
        jd_text = extract_text_from_file(file_path)
        if not jd_text or len(jd_text) < 20:
            logging.warning(f"Could not extract meaningful text from {filename}. Skipping.")
            continue
            
        logging.info(f"Extracted {len(jd_text)} characters from {filename}.")

        # Check if dropped file is an Email List / Cold Outreach Sheet (no job post)
        import cold_email_agent
        if cold_email_agent.is_email_list_source(jd_text, file_path=file_path):
            logging.info(f"[ColdEmail Classifier] Detected Cold Email List in {filename}. Routing to Direct Cold Outreach Engine...")
            added, skipped = cold_email_agent.ingest_source(file_path, config=config, logs_dir=logs_dir)
            logging.info(f"[ColdEmail Classifier] Enqueued {added} lead(s) from {filename} ({skipped} skipped/duplicate).")
            try:
                os.remove(file_path)
                logging.info(f"Cleaned up ingested cold email list dropzone file: {filename}")
            except Exception as e:
                logging.error(f"Error removing dropzone file {filename}: {e}")
            continue

        # Check if dropped file contains an external ATS job link (Greenhouse / Workday / Shortened redirect)
        import ats_bridge
        ats_type, ats_url = ats_bridge.find_ats_url_in_text(jd_text, resolve_shorteners=True)
        if ats_type and ats_url:
            logging.info(f"[ATS Classifier] Detected {ats_type.upper()} Job URL in {filename}: {ats_url}. Routing to {ats_type.title()} Agent...")
            ats_bridge.dispatch_ats_url(ats_type, ats_url, config)
            try:
                os.remove(file_path)
                logging.info(f"Cleaned up processed {ats_type.title()} dropzone file: {filename}")
            except Exception as e:
                logging.error(f"Error removing dropzone file {filename}: {e}")
            continue

        # Fast Pre-LLM Deduplication & Suppression Check (saves ~65s of Ollama + PDF rendering on duplicates)
        pre_emails = extract_valid_emails("", jd_text, sender_email)
        if pre_emails:
            pre_statuses = [
                (em, *can_send_email(em, config, sent_records, suppression_data, sender_email=sender_email))
                for em in pre_emails
            ]
            if not any(item[1] for item in pre_statuses):
                for em, _, code, detail in pre_statuses:
                    logging.info(f"[Pre-LLM Deduplication] {code}: {detail}")
                    record_skipped_email(logs_dir, em, code, sent_records)
                try:
                    os.remove(file_path)
                    logging.info(f"Cleaned up duplicate/suppressed dropzone file: {filename}")
                except Exception as e:
                    logging.error(f"Error removing dropzone file {filename}: {e}")
                continue
        
        # 1. Parse JD metadata with Ollama
        logging.info("Parsing Job Description metadata with local Ollama model...")
        jd_info = parse_jd_with_ollama(config, jd_text)
        raw_company = (jd_info.get('company_name') or '').strip()
        has_company = not is_unspecified_company(raw_company)
        clean_company = re.sub(r'[^\w\-]', '', raw_company.replace(' ', '_')) if has_company else ""
        
        recipients = jd_info.get('recipient_emails') or []
        if not recipients and jd_info.get('recipient_email'):
            recipients = [jd_info.get('recipient_email').strip()]
        
        logging.info(
            f"Extracted Metadata -> Company: {raw_company or '(Unspecified)'}, "
            f"Role: {jd_info.get('job_title')}, Recruiter: {jd_info.get('recruiter_name') or '(Team)'}, "
            f"Target Email(s): {recipients}"
        )

        # 1.5 Autonomous Lead Quality Scoring Gate
        target_em = recipients[0] if recipients else ""
        lead_eval = optimizer.score_lead_relevance(
            jd_info.get('job_title', ''),
            content_text=jd_text,
            company_name=raw_company,
            recruiter_email=target_em
        )
        logging.info(
            f"[Lead Quality Evaluator] Score: {lead_eval['score']}/100 ({lead_eval['tier']}) -> "
            f"{', '.join(lead_eval['reasons']) if lead_eval['reasons'] else 'Standard Match'}"
        )
        if lead_eval['action'] == 'DISCARD':
            logging.warning(f"[Lead Quality Evaluator] Discarding posting {filename} due to poor stack/seniority fit ({lead_eval['score']}/100).")
            try:
                os.remove(file_path)
            except Exception:
                pass
            continue
        
        # 2. Generate Tailored Alignment Paragraph (with Few-Shot + Quality Gate)
        logging.info("Generating AI-tailored cover letter alignment paragraph...")
        alignment_para = generate_tailored_paragraph(config, jd_info, jd_text)
        clean_alignment = clean_llm_text(alignment_para)
        
        # 3. Create Tailored PDFs with clean naming convention
        cand_prefix = re.sub(r'[^a-zA-Z0-9_-]', '_', profile.get('full_name', 'CANDIDATE')).upper()
        if clean_company:
            resume_pdf_filename = f"{cand_prefix}_{clean_company}.pdf"
            cover_pdf_filename = f"{cand_prefix}_COVER_LETTER_{clean_company}.pdf"
        else:
            resume_pdf_filename = f"{cand_prefix}_RESUME.pdf"
            cover_pdf_filename = f"{cand_prefix}_COVER_LETTER.pdf"
        
        resume_pdf_path = os.path.join(output_dir, resume_pdf_filename)
        cover_pdf_path = os.path.join(output_dir, cover_pdf_filename)
        
        logging.info(f"Rendering Tailored Resume PDF -> {resume_pdf_path}")
        tailored_summary = create_tailored_pdf_resume(resume_pdf_path, profile, jd_info, jd_text)
        
        logging.info(f"Rendering Tailored Cover Letter PDF -> {cover_pdf_path}")
        create_tailored_pdf_cover_letter(cover_pdf_path, profile, jd_info, clean_alignment)
        
        # 4. Select A/B/C Variant, Compose Multipart HTML+Plain Email & Dispatch
        display_title = clean_job_title(jd_info.get('job_title'))
        variant_id = select_email_variant(config, logs_dir)
        formal_email_body = compose_formal_email_body(
            profile, raw_company, display_title, clean_alignment, jd_info=jd_info, variant_id=variant_id
        )
        html_email_body = compose_html_email_body(formal_email_body, profile)
        
        valid_recipients = [r.strip() for r in recipients if r and re.match(r'^[\w\.\+\-]+@[\w\.\-]+\.[a-zA-Z]{2,}$', r.strip())]
        any_smtp_failed = False
        if valid_recipients:
            for recipient in valid_recipients:
                allowed, status_code, detail_msg = can_send_email(
                    recipient, config, sent_records, suppression_data, sender_email=sender_email
                )
                if not allowed:
                    logging.info(f"[Deduplication Guard] {status_code}: {detail_msg}")
                    record_skipped_email(logs_dir, recipient, status_code, sent_records)
                    continue
                # Dynamically resolve recruiter name specifically tailored to this recipient's email
                recipient_recruiter_name = extract_recruiter_first_name(
                    jd_info.get('raw_recruiter_name', ''),
                    recipient,
                    company_name=raw_company
                )
                recipient_jd_info = dict(jd_info, recruiter_name=recipient_recruiter_name)
                recipient_formal_body = compose_formal_email_body(
                    profile, raw_company, display_title, clean_alignment, jd_info=recipient_jd_info, variant_id=variant_id
                )
                recipient_html_body = compose_html_email_body(recipient_formal_body, profile)

                sent_ok = dispatch_gmail_smtp(
                    config,
                    recipient,
                    raw_company,
                    display_title,
                    recipient_formal_body,
                    resume_pdf_path,
                    cover_pdf_path,
                    jd_info=recipient_jd_info,
                    html_email_body=recipient_html_body
                )
                if not sent_ok:
                    any_smtp_failed = True
                    continue
                is_self_test = (status_code == "ALLOWED_SELF_TEST")
                if sent_ok and not is_self_test:
                    record_bandit_send(variant_id, logs_dir)
                    record_sent_email(
                        logs_dir,
                        recipient,
                        raw_company,
                        display_title,
                        sent_records,
                        email_variant=variant_id,
                        key_requirements=jd_info.get('key_requirements', []),
                        resume_summary=tailored_summary,
                        alignment_paragraph=clean_alignment
                    )
        else:
            logging.info(f"No valid recipient email detected for {raw_company or filename}. PDFs saved locally.")
        
        # 5. Clean up processed source file (preserve if SMTP failed so next run retries)
        if any_smtp_failed:
            logging.warning(f"Preserving {filename} in dropzone for automatic retry on next run due to SMTP error.")
        else:
            try:
                os.remove(file_path)
                logging.info(f"Cleaned up processed dropzone file: {filename}")
            except Exception as e:
                logging.error(f"Error removing dropzone file {filename}: {e}")
            
    # 6. Dispatch Queued Direct Cold Outreach Leads (Paced & Daily-Capped)
    try:
        import cold_email_agent
        cold_sent = cold_email_agent.dispatch_cold_email_queue(config=config, force=force_run)
        if cold_sent > 0:
            logging.info(f"Direct Cold Outreach dispatches this run: {cold_sent}")
    except Exception as e:
        logging.error(f"Error during cold email queue dispatch: {e}")

    logging.info("Job Automation Processing Run Completed.")

if __name__ == '__main__':
    process_dropzone()

