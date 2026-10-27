#!/usr/bin/env python3
"""
Autonomous Screenshot Intelligence & Intent Mining Engine (intent_miner.py)

Ingests screenshots from the dropzone and extracts:
  1. Full Job DNA: Title, normalized role archetype, seniority, primary & secondary tech stack,
     work mode, location, company tier, and recruiter hiring cues.
  2. Intent Memory & Knowledge Base: Deduplicates via SHA-256 and records entries into
     logs/screenshot_knowledge_base.json.
  3. Dynamic User Intent & Persona Profile: Updates logs/user_intent_profile.json using
     exponential temporal decay to model what kind of jobs Amrit is actively searching for.
  4. Lead Hunt Queue: Queues screenshots that lack direct emails into logs/lead_hunt_queue.json
     so linkedin_agent.py can hunt for recruiters/openings automatically.
"""

import os
import sys
import re
import json
import math
import hashlib
import logging
from datetime import datetime
from collections import Counter
import requests
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract

# Configure pytesseract binary path if needed
if os.path.exists('/opt/homebrew/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(LOGS_DIR, exist_ok=True)


def load_json_safe(file_path, default=None):
    """Safely load JSON data with a fallback default."""
    if default is None:
        default = {}
    if not os.path.exists(file_path):
        return default
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if data is not None else default
    except Exception as e:
        logging.debug(f"[IntentMiner] Could not load {file_path}: {e}")
        return default


def save_json_safe(file_path, data):
    """Safely serialize JSON data with atomic-like write."""
    try:
        temp_path = file_path + ".tmp"
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(temp_path, file_path)
        return True
    except Exception as e:
        logging.warning(f"[IntentMiner] Could not save {file_path}: {e}")
        return False


def compute_file_hash(file_path):
    """Compute SHA-256 hash of a file for exact deduplication."""
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        logging.debug(f"[IntentMiner] Hashing failed for {file_path}: {e}")
        return ""


def preprocess_screenshot_image(image_path):
    """
    Enhance screenshot image contrast and remove artifacts to maximize OCR accuracy.
    Returns a PIL Image object.
    """
    try:
        with Image.open(image_path) as img:
            img = img.convert('RGB')
            # Increase contrast slightly to make fine font weights legible
            enhancer = ImageEnhance.Contrast(img)
            img_enhanced = enhancer.enhance(1.4)
            # Sharpness boost
            sharpener = ImageEnhance.Sharpness(img_enhanced)
            img_sharp = sharpener.enhance(1.3)
            return img_sharp
    except Exception as e:
        logging.warning(f"[IntentMiner] Image preprocessing error for {image_path}: {e}")
        try:
            return Image.open(image_path)
        except Exception:
            return None


def extract_raw_ocr_text(file_path):
    """Run layout-preserving OCR on an image file."""
    preprocessed = preprocess_screenshot_image(file_path)
    if preprocessed is None:
        return ""
    try:
        # --psm 6 or 3 handles multi-column / card layouts well
        custom_config = r'--psm 3'
        raw_text = pytesseract.image_to_string(preprocessed, config=custom_config)
        return raw_text.strip()
    except Exception as e:
        logging.warning(f"[IntentMiner] OCR extraction failed for {file_path}: {e}")
        return ""


# -------------------------------------------------------------------------
# 1. DEEP SEMANTIC PARSING WITH OLLAMA
# -------------------------------------------------------------------------

INTENT_EXTRACTION_PROMPT = """
You are an advanced AI technical recruiting intelligence system.
Analyze the following raw OCR text extracted from a job post screenshot.
Your task is to extract comprehensive, high-fidelity Job DNA as a single valid JSON object.

Rules:
1. "job_title": The primary job title being advertised (e.g. "Senior Java Developer", "Full Stack Engineer").
2. "normalized_role": Normalize into one of:
   - "Java Backend / Microservices"
   - "Java Full Stack Developer"
   - "Python FastAPI / Backend"
   - "Full Stack Developer React"
   - "Software Development Engineer II"
   - "Cloud / Distributed Systems Engineer"
   - "Other Technical Role"
3. "seniority_level": "Junior (0-2y)", "Mid-Level (3-5y)", "Senior (5-8y)", or "Lead/Architect (8y+)".
4. "primary_tech_stack": Array of core backend/frontend frameworks or languages mentioned (e.g. ["Java 21", "Spring Boot", "Microservices", "React"]).
5. "secondary_tech_stack": Array of auxiliary tools, databases, or cloud infrastructure (e.g. ["Kafka", "PostgreSQL", "AWS", "Redis", "Docker", "Kubernetes"]).
6. "company_name": Name of hiring organization (or "" if unspecified).
7. "company_tier": "Product Startup", "High-Growth Scaleup", "Enterprise / MNC", "Fintech", "Staffing / Recruiter", or "Unknown".
8. "work_mode": "Remote", "Hybrid", "Onsite", or "Unspecified".
9. "locations": Array of cities mentioned (e.g. ["Bengaluru", "Remote", "Pune"]).
10. "experience_min": Minimum years of experience as a number (e.g. 3), or 0 if not stated.
11. "experience_max": Maximum years of experience as a number (e.g. 6), or 0 if not stated.
12. "recruiter_cues": Array of hiring action/urgency phrases found in the text (e.g. ["immediate joiner", "urgent requirement", "drop cv", "send resume", "DM me"]).
13. "recipient_email": Email address to apply to, or "" if none found.
14. "recruiter_name": Name of the recruiter/poster if identifiable, or "" if none.
15. "summary": A crisp 1-sentence overview of the role.

Return ONLY a valid JSON object matching this schema.

Job Post Text:
-----------------
{text}
-----------------
JSON Output:
"""


def extract_job_dna_with_ollama(config, text):
    """Extract structured Job DNA using local Ollama model."""
    host = config.get('ollama', {}).get('host', 'http://localhost:11434')
    model = config.get('ollama', {}).get('model', 'llama3.1:8b')
    url = f"{host.rstrip('/')}/api/generate"

    prompt = INTENT_EXTRACTION_PROMPT.format(text=text[:4500])
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1
        }
    }

    try:
        res = requests.post(url, json=payload, timeout=40)
        res.raise_for_status()
        raw_out = res.json().get('response', '')
        # Parse JSON
        m = re.search(r'\{.*\}', raw_out, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        return json.loads(raw_out)
    except Exception as e:
        logging.debug(f"[IntentMiner] Ollama deep extraction unavailable or timed out: {e}")
        return fallback_heuristic_extraction(text)


def fallback_heuristic_extraction(text):
    """High-accuracy deterministic fallback extractor when LLM is unavailable."""
    text_lower = text.lower()

    # 1. Tech stack detection
    tech_patterns = [
        "Java 21", "Java 17", "Java 11", "Java", "Spring Boot 3", "Spring Boot",
        "Microservices", "React 19", "React", "Next.js", "Python", "FastAPI",
        "PostgreSQL", "MySQL", "MongoDB", "Redis", "Kafka", "RabbitMQ", "AWS",
        "Docker", "Kubernetes", "gRPC", "GraphQL", "REST API", "Hibernate", "JPA",
        "Elasticsearch", "System Design", "Distributed Systems"
    ]
    detected_primary = []
    detected_secondary = []
    for t in tech_patterns:
        # Match word boundaries for short terms like Java or AWS
        pattern = r'\b' + re.escape(t.lower()) + r'\b'
        if re.search(pattern, text_lower):
            if t in ["Java 21", "Java", "Spring Boot", "Spring Boot 3", "Microservices", "React", "Python", "FastAPI"]:
                if t not in detected_primary:
                    detected_primary.append(t)
            else:
                if t not in detected_secondary:
                    detected_secondary.append(t)

    # 2. Recruiter cues
    cues = []
    for cue in ["immediate joiner", "urgent requirement", "serving notice", "0-15 days",
                "drop cv", "share resume", "send resume", "send cv", "reach out", "we are hiring", "hiring"]:
        if cue in text_lower:
            cues.append(cue)

    # 3. Work mode & locations
    work_mode = "Unspecified"
    if "remote" in text_lower or "work from home" in text_lower or "wfh" in text_lower:
        work_mode = "Remote"
    elif "hybrid" in text_lower:
        work_mode = "Hybrid"
    elif "onsite" in text_lower or "in-office" in text_lower:
        work_mode = "Onsite"

    locations = []
    for loc in ["Bengaluru", "Bangalore", "Hyderabad", "Pune", "Noida", "Gurgaon", "Gurugram", "Delhi NCR", "Mumbai", "Remote"]:
        if loc.lower() in text_lower:
            locations.append(loc)

    # 4. Email extraction
    email_match = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', text)
    recipient_email = email_match.group(0).lower() if email_match else ""

    # 5. Experience extraction
    exp_min = 3
    exp_max = 5
    exp_match = re.search(r'(\d+)\s*(?:-|to)\s*(\d+)\s*(?:years|yrs|yoe)', text_lower)
    if exp_match:
        try:
            exp_min = int(exp_match.group(1))
            exp_max = int(exp_match.group(2))
        except Exception:
            pass

    # 6. Normalized role
    if "full stack" in text_lower or ("java" in text_lower and "react" in text_lower):
        normalized_role = "Java Full Stack Developer"
    elif "python" in text_lower or "fastapi" in text_lower:
        normalized_role = "Python FastAPI / Backend"
    elif "java" in text_lower or "spring" in text_lower:
        normalized_role = "Java Backend / Microservices"
    else:
        normalized_role = "Other Technical Role"

    return {
        "job_title": normalized_role,
        "normalized_role": normalized_role,
        "seniority_level": "Mid-Level (3-5y)",
        "primary_tech_stack": detected_primary or ["Java", "Spring Boot"],
        "secondary_tech_stack": detected_secondary or ["Microservices", "PostgreSQL"],
        "company_name": "",
        "company_tier": "Unknown",
        "work_mode": work_mode,
        "locations": locations or ["Remote", "Bengaluru"],
        "experience_min": exp_min,
        "experience_max": exp_max,
        "recruiter_cues": cues,
        "recipient_email": recipient_email,
        "recruiter_name": "",
        "summary": "Heuristic fallback extracted job lead."
    }


# -------------------------------------------------------------------------
# 2. KNOWLEDGE BASE & INTENT PROFILING
# -------------------------------------------------------------------------

def ingest_screenshot_knowledge(file_path, config=None, logs_dir=LOGS_DIR):
    """
    Full pipeline to ingest a single screenshot file into the Knowledge Base:
      1. Preprocess & OCR text.
      2. Deduplicate using SHA-256 hash.
      3. Extract deep Job DNA via Ollama / Heuristics.
      4. Save record in logs/screenshot_knowledge_base.json.
      5. Recompute and evolve logs/user_intent_profile.json.
      6. If no email is found, register in logs/lead_hunt_queue.json.
    """
    if config is None:
        config = load_json_safe(CONFIG_PATH, {})

    filename = os.path.basename(file_path)
    file_hash = compute_file_hash(file_path)
    if not file_hash:
        logging.warning(f"[IntentMiner] Could not compute hash for {filename}. Skipping.")
        return None

    kb_path = os.path.join(logs_dir, 'screenshot_knowledge_base.json')
    kb_data = load_json_safe(kb_path, {
        "total_screenshots_processed": 0,
        "last_updated": None,
        "history": []
    })

    # Check for exact duplicate hash
    existing_hashes = {h.get('file_hash') for h in kb_data.get('history', []) if isinstance(h, dict)}
    if file_hash in existing_hashes:
        logging.info(f"[IntentMiner] ⏩ Screenshot {filename} has already been indexed (hash match). Skipping re-learning.")
        for item in kb_data.get('history', []):
            if item.get('file_hash') == file_hash:
                return item

    # Extract OCR text
    ocr_text = extract_raw_ocr_text(file_path)
    if not ocr_text or len(ocr_text) < 25:
        logging.warning(f"[IntentMiner] Insufficient OCR text extracted from {filename}.")
        return None

    # Deep semantic extraction
    job_dna = extract_job_dna_with_ollama(config, ocr_text)

    # Clean / sanitize fields
    has_email = bool(job_dna.get('recipient_email') and '@' in job_dna.get('recipient_email', ''))
    primary_tech = [t for t in job_dna.get('primary_tech_stack', []) if isinstance(t, str) and len(t) < 30]
    secondary_tech = [t for t in job_dna.get('secondary_tech_stack', []) if isinstance(t, str) and len(t) < 30]

    record = {
        "file_hash": file_hash,
        "filename": filename,
        "ingested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw_text_length": len(ocr_text),
        "job_dna": job_dna,
        "has_direct_email": has_email,
        "target_email": job_dna.get('recipient_email', '')
    }

    kb_data.setdefault('history', []).append(record)
    kb_data['total_screenshots_processed'] = len(kb_data['history'])
    kb_data['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_json_safe(kb_path, kb_data)

    logging.info(
        f"[IntentMiner] 🧠 Learned Job DNA from screenshot '{filename}': "
        f"Role='{job_dna.get('normalized_role')}', Primary={primary_tech[:4]}, "
        f"Email={'Yes (' + job_dna.get('recipient_email') + ')' if has_email else 'No direct email (Queued for Hunt)'}"
    )

    # If no email is present, register in Lead Hunt Queue
    if not has_email:
        register_in_lead_hunt_queue(job_dna, filename, logs_dir)

    # Recompute user intent profile
    profile = recompute_user_intent_profile(kb_data, logs_dir)

    return record


def register_in_lead_hunt_queue(job_dna, filename, logs_dir=LOGS_DIR):
    """Register screenshots that lack direct email so LinkedIn agent can hunt for the recruiter/company."""
    queue_path = os.path.join(logs_dir, 'lead_hunt_queue.json')
    queue = load_json_safe(queue_path, {
        "total_targets": 0,
        "last_updated": None,
        "targets": []
    })

    company = job_dna.get('company_name', '').strip()
    role = job_dna.get('job_title', '').strip() or job_dna.get('normalized_role', '')
    recruiter = job_dna.get('recruiter_name', '').strip()

    if not company and not role:
        return

    # Deduplicate against existing hunt targets
    for existing in queue.get('targets', []):
        if existing.get('company_name', '').lower() == company.lower() and existing.get('role', '').lower() == role.lower():
            return

    target = {
        "id": f"hunt_{datetime.now().strftime('%Y%m%d%H%M%S')}_{abs(hash(company + role)) % 10000}",
        "company_name": company,
        "role": role,
        "recruiter_hint": recruiter,
        "primary_tech": job_dna.get('primary_tech_stack', []),
        "source_screenshot": filename,
        "status": "PENDING_HUNT",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    queue.setdefault('targets', []).append(target)
    queue['total_targets'] = len(queue['targets'])
    queue['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_json_safe(queue_path, queue)
    logging.info(f"[IntentMiner] 🎯 Added '{role}' at '{company or '(Unknown Co)'}' to Lead Hunt Queue.")


def recompute_user_intent_profile(kb_data=None, logs_dir=LOGS_DIR):
    """
    Recompute the cumulative User Intent Profile from historical screenshots.
    Applies exponential decay so recent screenshots carry more weight.
    """
    if kb_data is None:
        kb_path = os.path.join(logs_dir, 'screenshot_knowledge_base.json')
        kb_data = load_json_safe(kb_path, {"history": []})

    history = kb_data.get("history", [])
    now = datetime.now()

    role_counter = Counter()
    tech_counter = Counter()
    cue_counter = Counter()
    work_mode_counter = Counter()
    location_counter = Counter()
    exp_mins = []
    exp_maxs = []

    # Half-life of 21 days for temporal recency decay
    LAMBDA_DECAY = math.log(2) / 21.0

    for item in history:
        if not isinstance(item, dict):
            continue
        dna = item.get("job_dna", {})
        if not isinstance(dna, dict):
            continue

        ts_str = item.get("ingested_at", "")
        weight = 1.0
        try:
            item_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            days_ago = max(0, (now - item_dt).total_seconds() / 86400.0)
            weight = math.exp(-LAMBDA_DECAY * days_ago)
        except Exception:
            weight = 1.0

        role = dna.get("normalized_role") or dna.get("job_title")
        if role:
            role_counter[role] += weight

        for tech in (dna.get("primary_tech_stack") or []):
            if tech and isinstance(tech, str):
                tech_counter[tech] += weight * 1.5

        for tech in (dna.get("secondary_tech_stack") or []):
            if tech and isinstance(tech, str):
                tech_counter[tech] += weight * 1.0

        for cue in (dna.get("recruiter_cues") or []):
            if cue and isinstance(cue, str):
                cue_counter[cue.lower()] += weight

        wm = dna.get("work_mode")
        if wm and wm != "Unspecified":
            work_mode_counter[wm] += weight

        for loc in (dna.get("locations") or []):
            if loc and isinstance(loc, str):
                location_counter[loc] += weight

        if dna.get("experience_min"):
            exp_mins.append(dna.get("experience_min"))
        if dna.get("experience_max"):
            exp_maxs.append(dna.get("experience_max"))

    # Baseline anchor weights for Amrit Kumar if dataset is still growing
    baseline_anchors = {
        "Java 21": 5.0,
        "Spring Boot 3": 4.5,
        "Microservices": 4.0,
        "React 19": 3.5,
        "PostgreSQL": 3.0,
        "AWS": 3.0,
        "Kafka": 2.5,
        "Python": 2.0,
        "FastAPI": 2.0,
        "Docker": 2.0
    }
    for k, v in baseline_anchors.items():
        tech_counter[k] += v

    total_role_weight = sum(role_counter.values()) or 1.0
    role_archetypes = [
        {"role": r, "weight": round(c / total_role_weight, 3), "raw_count": round(c, 1)}
        for r, c in role_counter.most_common(6)
    ]

    max_tech_c = max(tech_counter.values()) if tech_counter else 1.0
    normalized_skills = {
        k: round(v / max_tech_c, 3)
        for k, v in tech_counter.most_common(20)
    }

    avg_min_exp = round(sum(exp_mins) / len(exp_mins), 1) if exp_mins else 3.0
    avg_max_exp = round(sum(exp_maxs) / len(exp_maxs), 1) if exp_maxs else 5.5

    # Structure discrete Personas for search query synthesis
    personas = [
        {
            "id": "persona_java_microservices",
            "name": "Java Microservices & Cloud Architect",
            "primary_keywords": ["Java 21", "Spring Boot 3", "Microservices", "Kafka", "PostgreSQL", "AWS"],
            "weight": 0.50
        },
        {
            "id": "persona_fullstack_react",
            "name": "Full Stack Java + React Specialist",
            "primary_keywords": ["Java", "Spring Boot", "React 19", "TypeScript", "REST API", "Microservices"],
            "weight": 0.35
        },
        {
            "id": "persona_fastapi_python",
            "name": "Python FastAPI & Cloud Backend",
            "primary_keywords": ["Python", "FastAPI", "PostgreSQL", "AWS", "Microservices", "Docker"],
            "weight": 0.15
        }
    ]

    profile_data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sample_size_screenshots": len(history),
        "top_role_archetypes": role_archetypes,
        "weighted_skills": normalized_skills,
        "preferred_experience_range": {
            "min_years": max(2.5, avg_min_exp),
            "max_years": max(4.0, avg_max_exp)
        },
        "preferred_work_modes": [item[0] for item in work_mode_counter.most_common(3)] or ["Remote", "Hybrid"],
        "top_locations": [item[0] for item in location_counter.most_common(6)] or ["Bengaluru", "Remote", "Hyderabad", "Pune"],
        "recruiter_trigger_phrases": [item[0] for item in cue_counter.most_common(8)] or [
            "immediate joiner", "urgent requirement", "drop cv", "send resume", "share cv"
        ],
        "personas": personas,
        "discovered_negative_keywords": [
            ".net", "c#", "php", "wordpress", "drupal", "salesforce", "sap",
            "manual testing", "qa automation", "sdet", "data entry", "bpo", "internship only"
        ]
    }

    profile_path = os.path.join(logs_dir, 'user_intent_profile.json')
    save_json_safe(profile_path, profile_data)
    logging.info(
        f"[IntentMiner] 📊 Updated User Intent Profile: {len(role_archetypes)} roles, "
        f"{len(normalized_skills)} weighted skills from {len(history)} total screenshots."
    )
    return profile_data


# -------------------------------------------------------------------------
# 3. DIRECTORY BATCH PROCESSOR
# -------------------------------------------------------------------------

def process_pending_screenshots(dropzone_dir, config=None, logs_dir=LOGS_DIR):
    """
    Scans a directory for any screenshots (.png, .jpg, .jpeg, .webp)
    and ingests their intelligence into the Knowledge Base.
    """
    if not os.path.exists(dropzone_dir):
        return []

    valid_exts = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
    results = []
    for fname in os.listdir(dropzone_dir):
        ext = os.path.splitext(fname)[1].lower()
        if ext in valid_exts:
            full_path = os.path.join(dropzone_dir, fname)
            res = ingest_screenshot_knowledge(full_path, config=config, logs_dir=logs_dir)
            if res:
                results.append(res)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    test_dropzone = os.path.expanduser('~/Documents/JobAutomation/dropzone')
    print("Testing Intent Miner on dropzone:", test_dropzone)
    recompute_user_intent_profile()
    print("Intent Profile initialized successfully.")
