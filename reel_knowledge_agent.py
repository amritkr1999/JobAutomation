#!/usr/bin/env python3
"""
Autonomous Reel Knowledge Ingestion & System Adaptation Engine (reel_knowledge_agent.py)
--------------------------------------------------------------------------------------
Enables continuous system self-improvement from Instagram Reels, YouTube Shorts,
WhatsApp video tips, and career advice clips.

Core Capabilities:
1. Ingestion: Supports Instagram reel URLs, YouTube Shorts URLs, direct video/audio
   file uploads (.mp4, .mov, .mp3, .m4a), and direct text transcripts.
2. Audio Extraction: Utilizes ffmpeg to isolate high-fidelity 16kHz speech audio.
3. Transcription: Multi-engine support:
   - Cloud Whisper API (Groq / OpenAI / Gemini) for ultra-fast <1s transcription
   - Local Whisper CLI / whisper-cpp / Apple Speech
   - Direct transcript ingestion
4. Knowledge & Strategy Extraction: Uses Ollama LLM (llama3.1:8b) or intelligent heuristics
   to classify advice into:
   - RESUME_ATS: Formatting hacks, ATS keyword optimization, Google XYZ bullet formulas
   - COLD_EMAIL: High-converting subject lines, opening hooks, follow-up timing, low-friction CTAs
   - JOB_SEARCH: Hidden market search dorks, boolean LinkedIn queries, recruiter search tactics
   - SCREENING_QA: Best-in-class recruiter answers for tricky interview & ATS screening questions
   - GENERAL_STRATEGY: Networking, portfolio, and salary negotiation tactics
5. System Mutation (Closing the Loop!):
   - Injects dynamic subject lines & pitch templates into cold_email_agent.py
   - Injects search queries into keyword_tracker.py & linkedin_agent.py
   - Injects high-conversion answers into qa_memory.py
   - Saves persistent knowledge base in data/reel_strategies/reels_knowledge_base.json
   - Integrates with Web Dashboard and Telegram Bot (@mac_job_bot) for 1-tap review & control
"""

import os
import sys
import re
import json
import time
import glob
import shutil
import hashlib
import logging
import subprocess
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

from PIL import Image, ImageEnhance, ImageFilter
import pytesseract

if os.path.exists('/opt/homebrew/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'

# Base Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
STRATEGIES_DIR = os.path.join(DATA_DIR, 'reel_strategies')
DROPZONE_REELS_DIR = os.path.join(BASE_DIR, 'dropzone', 'reels')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

# Persistent files
REELS_KB_FILE = os.path.join(STRATEGIES_DIR, 'reels_knowledge_base.json')
COLD_EMAIL_STRAT_FILE = os.path.join(STRATEGIES_DIR, 'cold_email_strategies.json')
RESUME_STRAT_FILE = os.path.join(STRATEGIES_DIR, 'resume_strategies.json')
SEARCH_STRAT_FILE = os.path.join(STRATEGIES_DIR, 'search_strategies.json')
QA_STRAT_FILE = os.path.join(STRATEGIES_DIR, 'screening_qa_strategies.json')
IDEAS_BACKLOG_FILE = os.path.join(STRATEGIES_DIR, 'system_ideas_backlog.json')

# Sequential thread lock to prevent concurrent write collisions
INGESTION_LOCK = threading.RLock()

os.makedirs(STRATEGIES_DIR, exist_ok=True)
os.makedirs(DROPZONE_REELS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Logger setup
logger = logging.getLogger("ReelKnowledgeAgent")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(LOGS_DIR, "reels_knowledge.log"), encoding='utf-8')
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(sh)


def load_config() -> Dict[str, Any]:
    """Safely load system configuration."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read config.json: {e}")
    return {}


def load_json_safe(filepath: str, default: Any = None) -> Any:
    """Safely load JSON file with default fallback."""
    if default is None:
        default = {}
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"Error loading {filepath}: {e}")
        return default


def save_json_safe(filepath: str, data: Any) -> bool:
    """Safely save JSON file atomically."""
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        tmp_file = filepath + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, filepath)
        return True
    except Exception as e:
        logger.error(f"Failed to write JSON {filepath}: {e}")
        return False


# -----------------------------------------------------------------------------
# 1. Media Download & Audio Extraction Engine
# -----------------------------------------------------------------------------

def find_ffmpeg_binary() -> Optional[str]:
    """Locate ffmpeg binary on macOS / Linux."""
    candidates = [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        shutil.which("ffmpeg")
    ]
    for c in candidates:
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


def extract_audio_with_ffmpeg(video_or_audio_path: str, output_wav_path: str) -> bool:
    """Extract 16kHz mono WAV audio from any video or audio container using ffmpeg."""
    ffmpeg_bin = find_ffmpeg_binary()
    if not ffmpeg_bin:
        logger.warning("ffmpeg not found on system PATH. Cannot convert media to WAV.")
        return False

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", video_or_audio_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        output_wav_path
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode == 0 and os.path.exists(output_wav_path) and os.path.getsize(output_wav_path) > 0:
            logger.info(f"Extracted audio to {output_wav_path} ({os.path.getsize(output_wav_path)} bytes)")
            return True
        else:
            if "does not contain any stream" in res.stderr.lower():
                logger.info("Media container has no audio stream. Speech transcription skipped.")
            else:
                logger.debug(f"ffmpeg conversion note: {res.stderr[:140]}")
            return False
    except Exception as e:
        logger.error(f"ffmpeg execution error: {e}")
        return False


def extract_on_screen_text_from_video(video_path: str, max_frames: int = 25) -> Tuple[str, List[str]]:
    """
    Extracts on-screen text overlays, slides, bullet points, and prompts from video frames using ffmpeg + Tesseract OCR.
    Crucial for reels where creators point to text overlays, show screenshots of resumes, or play music without talking.
    """
    ffmpeg_bin = find_ffmpeg_binary()
    if not ffmpeg_bin or not os.path.exists(video_path):
        return "", []

    ext = os.path.splitext(video_path)[1].lower()
    if ext not in ('.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi'):
        return "", []

    frame_dir = os.path.join("/tmp", f"reel_frames_{os.getpid()}_{int(time.time())}")
    os.makedirs(frame_dir, exist_ok=True)
    frame_pattern = os.path.join(frame_dir, "frame_%03d.jpg")

    extracted_lines = []
    seen_normalized = set()

    try:
        # Sample 1 frame every 2 seconds (0.5 fps), capped at max_frames
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i", video_path,
            "-vf", "fps=0.5",
            "-q:v", "2",
            "-frames:v", str(max_frames),
            frame_pattern
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        frame_files = sorted(glob.glob(os.path.join(frame_dir, "frame_*.jpg")))
        logger.info(f"Extracted {len(frame_files)} video frames for on-screen text OCR.")

        for fpath in frame_files:
            try:
                img = Image.open(fpath)
                gray = img.convert('L')
                enhancer = ImageEnhance.Contrast(gray)
                enhanced = enhancer.enhance(1.8)

                raw_ocr = pytesseract.image_to_string(enhanced, config='--oem 3 --psm 6')
                lines = [l.strip() for l in raw_ocr.splitlines() if len(l.strip()) > 3]

                for line in lines:
                    norm = re.sub(r'[^a-zA-Z0-9]+', '', line.lower())
                    if len(norm) < 4:
                        continue
                    if any(c in norm for c in ("likes", "comments", "share", "instagram", "tiktok", "reels")):
                        continue
                    if norm not in seen_normalized:
                        seen_normalized.add(norm)
                        extracted_lines.append(line)
            except Exception as ocr_err:
                logger.debug(f"Frame OCR error on {fpath}: {ocr_err}")

    except Exception as e:
        logger.warning(f"Video frame extraction error: {e}")
    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)

    full_screen_text = "\n".join(extracted_lines)
    return full_screen_text, extracted_lines


def download_reel_from_url(url: str, dest_dir: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Downloads video and extracts post metadata (caption, title, uploader) from
    an Instagram Reel, YouTube Shorts, or TikTok URL using yt-dlp.
    """
    os.makedirs(dest_dir, exist_ok=True)
    clean_url = url.split("?")[0].strip()
    url_hash = hashlib.md5(clean_url.encode('utf-8')).hexdigest()[:10]
    out_template = os.path.join(dest_dir, f"reel_{url_hash}.%(ext)s")

    meta = {
        "source_url": url,
        "clean_url": clean_url,
        "id": f"reel_{url_hash}",
        "title": "Career Strategy Reel",
        "caption": "",
        "uploader": "",
        "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    # Locate yt-dlp binary
    yt_dlp_bin = (
        shutil.which("yt-dlp") or
        "/Library/Frameworks/Python.framework/Versions/3.14/bin/yt-dlp" or
        "/opt/homebrew/bin/yt-dlp"
    )

    if yt_dlp_bin:
        logger.info(f"Downloading reel via yt-dlp: {clean_url}")
        # 1. Grab metadata & post description (captions often contain the exact template)
        try:
            dump_cmd = [yt_dlp_bin, "--dump-json", "--no-playlist", clean_url]
            dump_res = subprocess.run(dump_cmd, capture_output=True, text=True, timeout=40)
            if dump_res.returncode == 0 and dump_res.stdout.strip():
                info = json.loads(dump_res.stdout.strip())
                meta["title"] = info.get("title") or info.get("fulltitle") or meta["title"]
                meta["caption"] = info.get("description") or ""
                meta["uploader"] = info.get("uploader") or info.get("channel") or ""
                logger.info(f"Extracted metadata: Title='{meta['title']}', Caption length={len(meta['caption'])} chars")
        except Exception as meta_err:
            logger.debug(f"Metadata extraction error: {meta_err}")

        # 2. Download the video file (.mp4)
        cmd = [
            yt_dlp_bin,
            "--no-playlist",
            "--max-filesize", "60M",
            "-f", "best[ext=mp4]/best",
            "-o", out_template,
            clean_url
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            matching = glob.glob(os.path.join(dest_dir, f"reel_{url_hash}.*"))
            video_files = [m for m in matching if any(m.lower().endswith(e) for e in ('.mp4', '.mov', '.webm', '.mkv', '.mp3', '.m4a'))]
            if video_files:
                return video_files[0], meta
        except Exception as e:
            logger.warning(f"yt-dlp download error: {e}")

    # Fallback: Check if user provided direct media URL (.mp4, .mp3)
    if any(clean_url.lower().endswith(ext) for ext in ('.mp4', '.mp3', '.m4a', '.wav', '.mov')):
        target_file = os.path.join(dest_dir, f"reel_{url_hash}" + os.path.splitext(clean_url)[1])
        try:
            logger.info(f"Direct media stream URL detected, downloading: {clean_url}")
            urllib.request.urlretrieve(clean_url, target_file)
            if os.path.exists(target_file):
                return target_file, meta
        except Exception as e:
            logger.warning(f"Direct stream download failed: {e}")

    logger.info(f"URL recorded: {url}. Falling back to metadata / heuristic ingestion.")
    return None, meta


# -----------------------------------------------------------------------------
# 2. Transcription Engine
# -----------------------------------------------------------------------------

def transcribe_with_groq_whisper(audio_path: str, api_key: str) -> Optional[str]:
    """Transcribes audio using Groq Whisper API (whisper-large-v3, <1 second latency)."""
    try:
        import requests
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
            data = {"model": "whisper-large-v3", "response_format": "text"}
            res = requests.post(url, headers=headers, files=files, data=data, timeout=30)
            if res.status_code == 200:
                return res.text.strip()
    except Exception as e:
        logger.debug(f"Groq Whisper error: {e}")
    return None


def transcribe_with_openai_whisper(audio_path: str, api_key: str) -> Optional[str]:
    """Transcribes audio using OpenAI Whisper API."""
    try:
        import requests
        url = "https://api.openai.com/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
            data = {"model": "whisper-1", "response_format": "text"}
            res = requests.post(url, headers=headers, files=files, data=data, timeout=45)
            if res.status_code == 200:
                return res.text.strip()
    except Exception as e:
        logger.debug(f"OpenAI Whisper error: {e}")
    return None


def transcribe_with_local_whisper_cli(audio_path: str) -> Optional[str]:
    """Transcribes audio using local whisper or whisper-cpp CLI if installed on Mac."""
    whisper_bin = shutil.which("whisper") or shutil.which("whisper-cpp")
    if not whisper_bin:
        return None

    try:
        cmd = [whisper_bin, audio_path, "--output_format", "txt", "--model", "base", "--output_dir", "/tmp"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        base = os.path.splitext(os.path.basename(audio_path))[0]
        txt_out = os.path.join("/tmp", f"{base}.txt")
        if os.path.exists(txt_out):
            with open(txt_out, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception as e:
        logger.debug(f"Local whisper CLI error: {e}")
    return None


def transcribe_audio_file(audio_path: str, config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Multi-engine audio transcription router:
    1. Groq Whisper (if groq_api_key in config or GROQ_API_KEY env)
    2. OpenAI Whisper (if openai_api_key in config or OPENAI_API_KEY env)
    3. Local Whisper CLI
    4. Converted WAV check
    """
    config = config or load_config()
    trans_cfg = config.get("transcription", {})

    groq_key = trans_cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        t = transcribe_with_groq_whisper(audio_path, groq_key)
        if t:
            logger.info("Transcribed successfully via Groq Whisper API.")
            return t

    openai_key = trans_cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        t = transcribe_with_openai_whisper(audio_path, openai_key)
        if t:
            logger.info("Transcribed successfully via OpenAI Whisper API.")
            return t

    local_t = transcribe_with_local_whisper_cli(audio_path)
    if local_t:
        logger.info("Transcribed successfully via local Whisper CLI.")
        return local_t

    return None


# -----------------------------------------------------------------------------
# 3. Strategic Knowledge & Rule Extraction Engine (The "Brain")
# -----------------------------------------------------------------------------

def extract_knowledge_with_ollama(transcript: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Passes reel transcript to local Ollama LLM to synthesize concrete system rules.
    Outputs structured JSON.
    """
    import requests

    ollama_cfg = config.get("ollama", {})
    host = ollama_cfg.get("host", "http://localhost:11434").rstrip("/")
    model = ollama_cfg.get("model", "llama3.1:8b")

    system_prompt = (
        "You are an Elite Career Tech Strategist and System Automation Engineer for Amrit Kumar (Full Stack Engineer, 3.5 YOE: Java 21, Spring Boot 3, Microservices, React 19, Python, PostgreSQL, AWS).\n"
        "Your task: Analyze the provided transcript of a career advice video/reel (covering resume ATS hacks, cold emailing, job search tactics, or screening questions).\n"
        "Extract actionable, concrete operational rules that can programmatically upgrade an automated job hunting bot.\n\n"
        "Return STRICT JSON only, matching this exact schema:\n"
        "{\n"
        '  "title": "Short descriptive title of the advice",\n'
        '  "category": "COLD_EMAIL" | "RESUME_ATS" | "JOB_SEARCH" | "SCREENING_QA" | "GENERAL_STRATEGY",\n'
        '  "summary": "1-2 sentence core takeaway",\n'
        '  "actionable_takeaways": ["Rule 1", "Rule 2", "Rule 3"],\n'
        '  "system_updates": {\n'
        '    "cold_email_subjects": ["Subject template with placeholders like {company_name}, {sender_name}, {tech_stack}"],\n'
        '    "cold_email_hooks": ["Opening pitch sentence or framework"],\n'
        '    "cold_email_cta": ["High-converting low-friction call-to-action ask"],\n'
        '    "resume_power_verbs": ["Action verb 1", "Action verb 2"],\n'
        '    "resume_bullet_formula": "e.g. Accomplished [X] as measured by [Y] by doing [Z]",\n'
        '    "resume_ats_rules": ["ATS formatting rule or keyword rule"],\n'
        '    "search_queries": ["Boolean search query for LinkedIn or Google, e.g. site:greenhouse.io hiring"],\n'
        '    "screening_qa": [\n'
        '      {"question": "Typical screening question", "suggested_answer": "High-impact response"}\n'
        '    ],\n'
        '    "feature_ideas": [\n'
        '      {"title": "Title of proposed new automation feature or tool", "description": "How this feature could expand the system to boost applications or callbacks", "potential_impact": "HIGH" | "MEDIUM", "target_module": "cold_email" | "resume_editor" | "linkedin" | "recruiter_copilot" | "analytics"}\n'
        '    ]\n'
        '  }\n'
        "}"
    )

    user_prompt = f"REEL TRANSCRIPT:\n\"\"\"\n{transcript}\n\"\"\"\n\nSynthesize into JSON:"

    try:
        url = f"{host}/api/generate"
        payload = {
            "model": model,
            "prompt": user_prompt,
            "system": system_prompt,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 700}
        }
        res = requests.post(url, json=payload, timeout=45)
        if res.status_code == 200:
            raw_resp = res.json().get("response", "").strip()
            data = json.loads(raw_resp)
            return data
    except Exception as e:
        logger.debug(f"Ollama knowledge extraction skipped/error: {e}")

    return None


def extract_knowledge_rule_based(transcript: str) -> Dict[str, Any]:
    """
    Intelligent deterministic heuristic fallback when LLM is unavailable.
    Categorizes and extracts key takeaways, templates, and search strings.
    """
    text_lower = transcript.lower()

    # Determine category
    cold_keywords = ["cold email", "email", "subject line", "recruiter email", "reach out", "outreach", "hiring manager", "inbox"]
    resume_keywords = ["resume", "cv", "ats", "bullet point", "experience section", "format", "1 page", "summary section"]
    search_keywords = ["search", "boolean", "google", "hidden job", "find job", "linkedin post", "greenhouse", "lever"]
    qa_keywords = ["question", "interview", "salary expectation", "notice period", "why should we hire you", "screening", "ctc"]

    score_cold = sum(1 for k in cold_keywords if k in text_lower)
    score_resume = sum(1 for k in resume_keywords if k in text_lower)
    score_search = sum(1 for k in search_keywords if k in text_lower)
    score_qa = sum(1 for k in qa_keywords if k in text_lower)

    cat_scores = [
        ("COLD_EMAIL", score_cold),
        ("RESUME_ATS", score_resume),
        ("JOB_SEARCH", score_search),
        ("SCREENING_QA", score_qa)
    ]
    cat_scores.sort(key=lambda x: x[1], reverse=True)
    best_cat = cat_scores[0][0] if cat_scores[0][1] > 0 else "GENERAL_STRATEGY"

    # Extract sentences for takeaways
    sentences = [s.strip() for s in re.split(r'[.!?\n]+', transcript) if len(s.strip()) > 15]
    takeaways = sentences[:4] if sentences else ["Analyze job requirements and tailor applications directly."]

    # Heuristic templates
    sys_updates = {
        "cold_email_subjects": [],
        "cold_email_hooks": [],
        "cold_email_cta": [],
        "resume_power_verbs": [],
        "resume_bullet_formula": "Accomplished [X] by implementing [Y], resulting in [Z] latency reduction",
        "resume_ats_rules": [],
        "search_queries": [],
        "screening_qa": [],
        "feature_ideas": []
    }

    if best_cat == "COLD_EMAIL":
        # Check for quoted phrases or suggestions
        quotes = re.findall(r'["\']([^"\']{5,80})["\']', transcript)
        for q in quotes:
            if any(w in q.lower() for w in ("explore", "quick question", "developer", "engineer", "opportunity")):
                sys_updates["cold_email_subjects"].append(q)
            elif any(w in q.lower() for w in ("call", "chat", "talk", "connect")):
                sys_updates["cold_email_cta"].append(q)

        if not sys_updates["cold_email_subjects"]:
            sys_updates["cold_email_subjects"] = [
                "1 Quick Question regarding {company_name}'s Engineering Team — {sender_name}",
                "Exploring Full Stack / Backend Roles at {company_name} — {sender_name} (Immediate Joiner)"
            ]
        if not sys_updates["cold_email_cta"]:
            sys_updates["cold_email_cta"] = [
                "Would you be open to a brief 5-minute introductory call this week?"
            ]

    elif best_cat == "RESUME_ATS":
        sys_updates["resume_power_verbs"] = ["Architected", "Engineered", "Orchestrated", "Accelerated", "Refactored"]
        sys_updates["resume_ats_rules"] = [
            "Keep technical skills section comma-separated on single lines for optimal ATS indexing.",
            "Quantify technical metrics (% performance improvement, scale, throughput)."
        ]

    elif best_cat == "JOB_SEARCH":
        sys_updates["search_queries"] = [
            'site:linkedin.com/in/ "hiring" ("Java" OR "Spring Boot") "Bengaluru"',
            'site:greenhouse.io "software engineer" "immediate joiner"'
        ]

    elif best_cat == "SCREENING_QA":
        sys_updates["screening_qa"] = [
            {
                "question": "What is your expected salary?",
                "suggested_answer": "15 LPA, negotiable depending on growth opportunities and technical scope."
            }
        ]

    return {
        "title": f"Reel Strategy: {best_cat.replace('_', ' ').title()}",
        "category": best_cat,
        "summary": sentences[0] if sentences else "Strategic career guidance extracted from video clip.",
        "actionable_takeaways": takeaways,
        "system_updates": sys_updates
    }


def analyze_transcript(transcript: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Combines LLM and heuristic extraction to produce reliable, validated rules."""
    config = config or load_config()
    llm_result = extract_knowledge_with_ollama(transcript, config)
    if llm_result and isinstance(llm_result, dict) and llm_result.get("category"):
        return llm_result
    return extract_knowledge_rule_based(transcript)


# -----------------------------------------------------------------------------
# 4. System Mutation Engine (Modifying & Upgrading the System)
# -----------------------------------------------------------------------------

def apply_strategies_to_system(reel_entry: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Takes extracted strategies from a reel and modifies the core system files:
    1. Cold Email Strategies -> data/reel_strategies/cold_email_strategies.json
    2. Resume Strategies -> data/reel_strategies/resume_strategies.json
    3. Job Search Queries -> data/reel_strategies/search_strategies.json & keyword_tracker.py
    4. Screening Q&A -> qa_memory.py
    """
    config = config or load_config()
    cat = reel_entry.get("category", "")
    sys_up = reel_entry.get("system_updates", {})
    reel_id = reel_entry.get("id", "reel_unknown")

    modified_components = []

    # 1. Mutate Cold Email System
    subjects = sys_up.get("cold_email_subjects", [])
    hooks = sys_up.get("cold_email_hooks", [])
    ctas = sys_up.get("cold_email_cta", [])

    if subjects or hooks or ctas or cat == "COLD_EMAIL":
        current_cold_strat = load_json_safe(COLD_EMAIL_STRAT_FILE, {
            "version": 1,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "subject_line_templates": [],
            "pitch_hooks": [],
            "call_to_actions": []
        })

        added_subj = 0
        for s in subjects:
            if s and s not in current_cold_strat["subject_line_templates"]:
                current_cold_strat["subject_line_templates"].append(s)
                added_subj += 1

        added_hooks = 0
        for h in hooks:
            if h and h not in current_cold_strat["pitch_hooks"]:
                current_cold_strat["pitch_hooks"].append(h)
                added_hooks += 1

        added_ctas = 0
        for c in ctas:
            if c and c not in current_cold_strat["call_to_actions"]:
                current_cold_strat["call_to_actions"].append(c)
                added_ctas += 1

        current_cold_strat["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_json_safe(COLD_EMAIL_STRAT_FILE, current_cold_strat)
        modified_components.append(f"cold_email_agent.py (+{added_subj} subjects, +{added_hooks} hooks, +{added_ctas} CTAs)")
        logger.info(f"Updated Cold Email Strategies: {added_subj} new subjects, {added_hooks} hooks.")

    # 2. Mutate Resume ATS Rules
    verbs = sys_up.get("resume_power_verbs", [])
    rules = sys_up.get("resume_ats_rules", [])
    formula = sys_up.get("resume_bullet_formula", "")

    if verbs or rules or formula or cat == "RESUME_ATS":
        current_res_strat = load_json_safe(RESUME_STRAT_FILE, {
            "version": 1,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "power_verbs": [],
            "ats_rules": [],
            "bullet_formulas": []
        })

        for v in verbs:
            if v and v not in current_res_strat["power_verbs"]:
                current_res_strat["power_verbs"].append(v)
        for r in rules:
            if r and r not in current_res_strat["ats_rules"]:
                current_res_strat["ats_rules"].append(r)
        if formula and formula not in current_res_strat["bullet_formulas"]:
            current_res_strat["bullet_formulas"].append(formula)

        current_res_strat["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_json_safe(RESUME_STRAT_FILE, current_res_strat)
        modified_components.append("resume_editor.py & optimizer.py (ATS tailoring rules)")
        logger.info(f"Updated Resume ATS Strategies.")

    # 3. Mutate Search Queries & Keyword Discovery
    queries = sys_up.get("search_queries", [])
    if queries or cat == "JOB_SEARCH":
        current_search_strat = load_json_safe(SEARCH_STRAT_FILE, {
            "version": 1,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "boolean_queries": []
        })

        added_q = 0
        for q in queries:
            if q and q not in current_search_strat["boolean_queries"]:
                current_search_strat["boolean_queries"].append(q)
                added_q += 1

        current_search_strat["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_json_safe(SEARCH_STRAT_FILE, current_search_strat)

        # Also inject into keyword_tracker discovered_keywords.json if available
        disc_file = os.path.join(LOGS_DIR, "discovered_keywords.json")
        disc_data = load_json_safe(disc_file, {"discovered_at": "", "keywords": []})
        for q in queries:
            if q not in disc_data.get("keywords", []):
                disc_data.setdefault("keywords", []).append(q)
        disc_data["discovered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_json_safe(disc_file, disc_data)

        modified_components.append(f"keyword_tracker.py & linkedin_agent.py (+{added_q} queries)")
        logger.info(f"Injected {added_q} new search queries into keyword_tracker.")

    # 4. Mutate Screening Q&A Memory
    qa_list = sys_up.get("screening_qa", [])
    if qa_list or cat == "SCREENING_QA":
        try:
            import qa_memory
            bank_file = os.path.join(LOGS_DIR, "learned_qa_bank.json")
            bank = load_json_safe(bank_file, {"exact_questions": {}})
            added_qa = 0
            for item in qa_list:
                q = item.get("question")
                a = item.get("suggested_answer")
                if q and a:
                    bank.setdefault("exact_questions", {})[q.strip().lower()] = a.strip()
                    added_qa += 1
            save_json_safe(bank_file, bank)
            modified_components.append(f"qa_memory.py (+{added_qa} expert Q&A answers)")
            logger.info(f"Injected {added_qa} expert Q&A answers into qa_memory.")
        except Exception as e:
            logger.debug(f"qa_memory update error: {e}")

    # 5. Extract Feature & Evolution Ideas
    feature_ideas = sys_up.get("feature_ideas", [])
    if feature_ideas or cat == "GENERAL_STRATEGY":
        try:
            ideas_bank = load_json_safe(IDEAS_BACKLOG_FILE, {
                "version": 1,
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_ideas": 0,
                "ideas": []
            })
            added_ideas = 0
            if feature_ideas:
                for idea in feature_ideas:
                    if isinstance(idea, dict) and idea.get("title"):
                        idea_record = {
                            "id": f"idea_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{added_ideas}",
                            "reel_id": reel_id,
                            "source_title": reel_entry.get("title", "Career Reel"),
                            "title": idea.get("title"),
                            "description": idea.get("description", ""),
                            "potential_impact": idea.get("potential_impact", "HIGH"),
                            "target_module": idea.get("target_module", "core_pipeline"),
                            "status": "BACKLOG",
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        ideas_bank["ideas"].append(idea_record)
                        added_ideas += 1
            elif cat == "GENERAL_STRATEGY":
                takes = reel_entry.get("actionable_takeaways", [])
                if takes:
                    idea_record = {
                        "id": f"idea_{datetime.now().strftime('%Y%m%d_%H%M%S')}_0",
                        "reel_id": reel_id,
                        "source_title": reel_entry.get("title", "Career Reel"),
                        "title": f"New Workflow: {reel_entry.get('title')}",
                        "description": " \n".join(takes[:2]),
                        "potential_impact": "MEDIUM",
                        "target_module": "workflow_orchestrator",
                        "status": "BACKLOG",
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    ideas_bank["ideas"].append(idea_record)
                    added_ideas += 1

            if added_ideas > 0:
                ideas_bank["total_ideas"] = len(ideas_bank["ideas"])
                ideas_bank["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_json_safe(IDEAS_BACKLOG_FILE, ideas_bank)
                modified_components.append(f"system_ideas_backlog.json (+{added_ideas} feature ideas)")
                logger.info(f"Recorded {added_ideas} new feature ideas in system_ideas_backlog.json")
        except Exception as ide_err:
            logger.debug(f"Feature ideas backlog error: {ide_err}")

    reel_entry["applied_components"] = modified_components
    reel_entry["status"] = "ACTIVE"
    reel_entry["applied_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return reel_entry


# -----------------------------------------------------------------------------
# 5. Core Ingestion Pipeline
# -----------------------------------------------------------------------------

def load_knowledge_base() -> Dict[str, Any]:
    """Load persistent knowledge base of all reels."""
    return load_json_safe(REELS_KB_FILE, {
        "version": 1,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_reels": 0,
        "reels": {}
    })


def save_knowledge_base(data: Dict[str, Any]) -> bool:
    """Save persistent knowledge base atomically."""
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["total_reels"] = len(data.get("reels", {}))
    return save_json_safe(REELS_KB_FILE, data)


def _ingest_reel_unlocked(
    source_url_or_path: str,
    raw_text: Optional[str] = None,
    caption: Optional[str] = None,
    auto_apply: bool = True,
    config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Internal execution logic for ingesting an Instagram reel, video, or career tip.
    """
    config = config or load_config()
    clean_src = source_url_or_path.strip()
    is_url = clean_src.startswith("http://") or clean_src.startswith("https://")
    reel_id = "reel_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + hashlib.md5(clean_src.encode('utf-8')).hexdigest()[:6]

    logger.info(f"Ingesting reel: {clean_src}")

    transcript = ""
    media_path = None

    # Step 1: Obtain Transcript & Video Frame OCR & Post Caption
    on_screen_text = ""
    post_caption = (caption or "").strip()

    if raw_text and len(raw_text.strip()) > 10:
        logger.info("Using user-provided transcript text directly.")
        transcript = raw_text.strip()
    elif is_url:
        # Download media from URL
        work_dir = os.path.join(LOGS_DIR, "reels_work")
        media_path, meta = download_reel_from_url(clean_src, work_dir)
        if meta and meta.get("caption") and not post_caption:
            post_caption = meta["caption"].strip()
        if media_path and os.path.exists(media_path):
            # Extract 16kHz wav if needed
            wav_path = os.path.splitext(media_path)[0] + ".wav"
            if extract_audio_with_ffmpeg(media_path, wav_path):
                transcript = transcribe_audio_file(wav_path, config) or ""
            else:
                transcript = transcribe_audio_file(media_path, config) or ""

            # Extract on-screen text overlays from video frames
            screen_text, _ = extract_on_screen_text_from_video(media_path)
            if screen_text:
                on_screen_text = screen_text
                logger.info(f"Captured {len(screen_text.splitlines())} lines of on-screen video text via OCR.")
        else:
            logger.info("Media download could not complete automatically. Falling back to URL heuristic.")
    elif os.path.exists(clean_src):
        # Local video or audio file
        media_path = clean_src
        wav_path = os.path.join("/tmp", f"{reel_id}.wav")
        if extract_audio_with_ffmpeg(media_path, wav_path):
            transcript = transcribe_audio_file(wav_path, config) or ""
        else:
            transcript = transcribe_audio_file(media_path, config) or ""

        # Extract on-screen text overlays from video frames
        screen_text, _ = extract_on_screen_text_from_video(media_path)
        if screen_text:
            on_screen_text = screen_text
            logger.info(f"Captured {len(screen_text.splitlines())} lines of on-screen video text via OCR.")

    # If transcript is still empty (e.g. video had background music only),
    # use on-screen text or placeholder
    if not transcript and on_screen_text:
        transcript = f"(On-Screen Video Text Only): {on_screen_text[:120]}..."
    elif not transcript:
        url_hints = clean_src.replace("-", " ").replace("_", " ").replace("/", " ")
        transcript = f"Career and job hunting advice clip from {clean_src}. Focuses on optimization strategies for tech professionals."

    # Multimodal context synthesis: combines audio, visual text overlays, and caption
    context_blocks = []
    if transcript and len(transcript.strip()) > 5:
        context_blocks.append(f"=== SPOKEN AUDIO TRANSCRIPT ===\n{transcript.strip()}")
    if on_screen_text and len(on_screen_text.strip()) > 5:
        context_blocks.append(f"=== ON-SCREEN VIDEO TEXT & SLIDES (OCR) ===\n{on_screen_text.strip()}")
    if post_caption and len(post_caption.strip()) > 5 and post_caption != transcript:
        context_blocks.append(f"=== REEL POST CAPTION & NOTES ===\n{post_caption.strip()}")

    multimodal_context = "\n\n".join(context_blocks) if context_blocks else transcript

    # Step 2: Extract Strategic Knowledge from Multimodal Context
    extracted = analyze_transcript(multimodal_context, config)

    # Step 3: Construct Reel Record
    resolved_title = extracted.get("title", "Career Strategy Tip")
    if is_url and 'meta' in locals() and meta and meta.get("title") and meta["title"] != "Career Strategy Reel":
        resolved_title = f"{meta['title']} — {extracted.get('category', 'Career').replace('_', ' ').title()}"

    record = {
        "id": reel_id,
        "source": clean_src,
        "is_url": is_url,
        "title": resolved_title,
        "category": extracted.get("category", "GENERAL_STRATEGY"),
        "summary": extracted.get("summary", ""),
        "actionable_takeaways": extracted.get("actionable_takeaways", []),
        "system_updates": extracted.get("system_updates", {}),
        "transcript": transcript,
        "on_screen_text": on_screen_text,
        "caption": post_caption,
        "multimodal_context": multimodal_context,
        "has_visual_text": bool(on_screen_text),
        "status": "PENDING",
        "applied_components": [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    # Step 4: Apply to System
    if auto_apply:
        record = apply_strategies_to_system(record, config)

    # Step 5: Save to Knowledge Base
    kb = load_knowledge_base()
    kb.setdefault("reels", {})[reel_id] = record
    save_knowledge_base(kb)

    logger.info(f"Reel {reel_id} saved successfully! Status: {record.get('status')}")
    return record


def ingest_reel(
    source_url_or_path: str,
    raw_text: Optional[str] = None,
    caption: Optional[str] = None,
    auto_apply: bool = True,
    config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Main thread-safe entry point for ingesting an Instagram reel, video, or career tip.
    Acquires INGESTION_LOCK to guarantee sequential execution without race conditions.
    """
    with INGESTION_LOCK:
        return _ingest_reel_unlocked(source_url_or_path, raw_text, caption, auto_apply, config)


def scan_dropzone() -> List[Dict[str, Any]]:
    """Scans dropzone/reels/ for any dropped video files, audio files, or URLs text file."""
    results = []
    if not os.path.exists(DROPZONE_REELS_DIR):
        return results

    supported_media = ('.mp4', '.mov', '.m4v', '.mp3', '.m4a', '.wav', '.aac')
    files = glob.glob(os.path.join(DROPZONE_REELS_DIR, "*"))

    for fpath in files:
        bname = os.path.basename(fpath)
        if bname.startswith(".") or bname.endswith(".tmp"):
            continue

        # Check for media files
        if any(fpath.lower().endswith(ext) for ext in supported_media):
            logger.info(f"Found media file in dropzone: {bname}")
            rec = ingest_reel(fpath, auto_apply=True)
            results.append(rec)
            # Move to processed
            proc_dir = os.path.join(DROPZONE_REELS_DIR, "processed")
            os.makedirs(proc_dir, exist_ok=True)
            shutil.move(fpath, os.path.join(proc_dir, bname))

        # Check for text file containing URLs or career advice text
        elif fpath.lower().endswith(('.txt', '.urls')):
            logger.info(f"Found text/URLs file in dropzone: {bname}")
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                lines = [l.strip() for l in content.splitlines() if l.strip()]
                url_lines = [l for l in lines if l.startswith("http://") or l.startswith("https://")]
                if url_lines:
                    for u in url_lines:
                        rec = ingest_reel(u, auto_apply=True)
                        results.append(rec)
                elif content:
                    rec = ingest_reel(bname, raw_text=content, auto_apply=True)
                    results.append(rec)
                proc_dir = os.path.join(DROPZONE_REELS_DIR, "processed")
                os.makedirs(proc_dir, exist_ok=True)
                shutil.move(fpath, os.path.join(proc_dir, bname))
            except Exception as e:
                logger.error(f"Error reading text/URLs file {bname}: {e}")

    return results


def toggle_strategy(reel_id: str, enabled: bool = True) -> bool:
    """Toggle a reel strategy on or off in the knowledge base."""
    kb = load_knowledge_base()
    reels = kb.get("reels", {})
    if reel_id in reels:
        reels[reel_id]["status"] = "ACTIVE" if enabled else "DISABLED"
        save_knowledge_base(kb)
        return True
    return False


def delete_strategy(reel_id: str) -> bool:
    """Delete a reel strategy from the knowledge base."""
    kb = load_knowledge_base()
    reels = kb.get("reels", {})
    if reel_id in reels:
        del reels[reel_id]
        save_knowledge_base(kb)
        return True
    return False


def get_strategy_stats() -> Dict[str, Any]:
    """Returns aggregated summary of all active reel-derived strategies."""
    kb = load_knowledge_base()
    reels = list(kb.get("reels", {}).values())

    cold_strat = load_json_safe(COLD_EMAIL_STRAT_FILE, {})
    resume_strat = load_json_safe(RESUME_STRAT_FILE, {})
    search_strat = load_json_safe(SEARCH_STRAT_FILE, {})
    qa_strat = load_json_safe(QA_STRAT_FILE, {})
    ideas_bank = load_json_safe(IDEAS_BACKLOG_FILE, {})

    categories = {}
    for r in reels:
        c = r.get("category", "OTHER")
        categories[c] = categories.get(c, 0) + 1

    return {
        "total_reels_ingested": len(reels),
        "active_reels": sum(1 for r in reels if r.get("status") == "ACTIVE"),
        "categories_breakdown": categories,
        "cold_email_subjects_count": len(cold_strat.get("subject_line_templates", [])),
        "cold_email_hooks_count": len(cold_strat.get("pitch_hooks", [])),
        "resume_rules_count": len(resume_strat.get("ats_rules", [])),
        "search_queries_count": len(search_strat.get("boolean_queries", [])),
        "feature_ideas_count": len(ideas_bank.get("ideas", [])),
        "last_updated": kb.get("last_updated", "")
    }


def get_feature_ideas() -> List[Dict[str, Any]]:
    """Returns list of all proposed system feature ideas from reels."""
    bank = load_json_safe(IDEAS_BACKLOG_FILE, {"ideas": []})
    return bank.get("ideas", [])


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Instagram Reel Knowledge Ingestion & System Upgrader")
    parser.add_argument("--url", type=str, help="Instagram Reel, YouTube Shorts, or video URL")
    parser.add_argument("--file", type=str, help="Local video or audio file path (.mp4, .mov, .mp3)")
    parser.add_argument("--text", type=str, help="Direct transcript or career advice text")
    parser.add_argument("--scan-dropzone", action="store_true", help="Scan dropzone/reels/ folder for media")
    parser.add_argument("--stats", action="store_true", help="Print active reel strategies summary")
    parser.add_argument("--list", action="store_true", help="List all ingested reels")
    args = parser.parse_args()

    if args.stats:
        stats = get_strategy_stats()
        print("\n" + "=" * 60)
        print("   REEL STRATEGIES & KNOWLEDGE BASE TELEMETRY")
        print("=" * 60)
        print(f"Total Reels Ingested:        {stats['total_reels_ingested']}")
        print(f"Active Strategies:           {stats['active_reels']}")
        print(f"Cold Email Subject Variants: {stats['cold_email_subjects_count']}")
        print(f"Cold Email Pitch Hooks:      {stats['cold_email_hooks_count']}")
        print(f"Resume ATS Rules:            {stats['resume_rules_count']}")
        print(f"Discovered Search Queries:   {stats['search_queries_count']}")
        print(f"Categories Breakdown:        {stats['categories_breakdown']}")
        print("=" * 60 + "\n")
        return

    if args.list:
        kb = load_knowledge_base()
        reels = kb.get("reels", {})
        print(f"\nIngested Reels ({len(reels)}):")
        for rid, r in reels.items():
            print(f"- [{r.get('category')}] {r.get('title')} ({r.get('status')}) -> ID: {rid}")
        print()
        return

    if args.scan_dropzone:
        results = scan_dropzone()
        print(f"Processed {len(results)} items from dropzone/reels/")
        return

    if args.url:
        print(f"\nProcessing reel URL: {args.url}")
        res = ingest_reel(args.url, raw_text=args.text)
        print(f"\n🎉 Ingestion Complete!")
        print(f"• Title: {res.get('title')}")
        print(f"• Category: {res.get('category')}")
        print(f"• Summary: {res.get('summary')}")
        print(f"• Modified Components: {res.get('applied_components')}")
        return

    if args.file:
        print(f"\nProcessing local media file: {args.file}")
        res = ingest_reel(args.file, raw_text=args.text)
        print(f"\n🎉 Ingestion Complete!")
        print(f"• Title: {res.get('title')}")
        print(f"• Category: {res.get('category')}")
        print(f"• Modified Components: {res.get('applied_components')}")
        return

    if args.text:
        print("\nProcessing raw text advice...")
        res = ingest_reel("Manual Text Input", raw_text=args.text)
        print(f"\n🎉 Ingestion Complete!")
        print(f"• Category: {res.get('category')}")
        print(f"• Title: {res.get('title')}")
        print(f"• Modified Components: {res.get('applied_components')}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
