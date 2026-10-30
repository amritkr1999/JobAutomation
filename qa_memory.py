#!/usr/bin/env python3
"""
Universal Persistent QA Memory Engine (qa_memory.py)
Captures, persists to disk, and automatically recalls user answers to all
job screening questions, demographic fields, sponsorship, and custom ATS forms.
Prevents repeating the same question across runs, platforms, and sessions.
"""

import os
import re
import json
import time
import logging
from typing import Optional, List, Dict, Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
QA_FILE = os.path.join(LOGS_DIR, 'learned_qa_bank.json')


def _normalize_key(text: str) -> str:
    """Normalize question text into a clean canonical lookup key."""
    if not text:
        return ""
    # Lowercase, replace non-alphanumeric with spaces, collapse spaces
    cleaned = re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()
    return cleaned


def load_qa_memory() -> Dict[str, Any]:
    """Load persistent learned Q&A bank from disk with default baselines."""
    os.makedirs(LOGS_DIR, exist_ok=True)

    default_data: Dict[str, Any] = {
        "version": 1,
        "last_updated": datetime_now_str(),
        "exact_questions": {
            # Pre-populated baseline candidate profile
            "do you now or will you in the future require sponsorship": "Yes",
            "do you now or will you in the future require sponsorship for an immigration related employment benefit": "Yes",
            "will you now or in the future require sponsorship for employment visa status": "Yes",
            "are you legally authorized to work in the united states": "No",
            "are you legally authorized to work in india": "Yes",
            "country of work authorization": "India",
            "preferred name": "Amrit",
            "first name": "Amrit",
            "last name": "Kumar",
            "name of current employer": "Tech Solutions Inc.",
            "current company": "Tech Solutions Inc.",
            "current organization": "Tech Solutions Inc.",
            "notice period": "7 days",
            "how soon can you join": "Immediately (7 days)",
            "current ctc": "12 LPA",
            "expected ctc": "15 LPA",
            "total years of experience": "3.5",
            "years of experience in java": "3",
            "years of experience in spring boot": "3",
            "years of experience in react": "3",
            "years of experience in python": "2",
            "years of experience in aws": "2",
            "years of experience in docker": "2",
            "years of experience in kubernetes": "2",
            "gender": "Male",
            "pronouns": "He/Him/His",
            "are you open to relocation": "Yes",
            "willing to relocate": "Yes",
            "how did you hear about this job": "LinkedIn",
            "linkedin profile": "https://linkedin.com/in/yourprofile",
            "github profile": "https://github.com/yourusername",
            "portfolio website": "https://github.com/yourusername"
        },
        "patterns": {
            # Keyword/phrase mappings
            "require sponsorship": "Yes",
            "visa sponsorship": "Yes",
            "immigration related": "Yes",
            "authorized to work in the u s": "No",
            "authorized to work in the us": "No",
            "authorized to work in the united states": "No",
            "authorized to work in india": "Yes",
            "preferred name": "Amrit",
            "current company": "Tech Solutions Inc.",
            "current employer": "Tech Solutions Inc.",
            "most recent company": "Tech Solutions Inc.",
            "present organization": "Tech Solutions Inc.",
            "notice period": "7 days",
            "immediate joiner": "Yes",
            "current ctc": "12 LPA",
            "expected ctc": "15 LPA",
            "total experience": "3.5",
            "willing to relocate": "Yes",
            "relocation": "Yes",
            "in office requirement": "Yes",
            "reside within": "Yes",
            "live within": "Yes",
            "how did you hear": "LinkedIn",
            "where did you hear": "LinkedIn",
            "why do you want to join": "I am excited about the opportunity to contribute to high-impact systems where resilience, high throughput, and clean architecture are paramount. My background in building distributed microservices with Java 21, Spring Boot 3, and React 19 aligns closely with engineering scalable platforms, and I look forward to bringing that same focus to your team's upcoming initiatives.",
            "what excites you": "Building high-performance distributed systems with modern tech stacks.",
            "accommodations": "None",
            "pronoun": "He/Him/His",
            "protected veteran": "I am not a protected veteran",
            "disability": "No, I do not have a disability"
        }
    }

    if os.path.exists(QA_FILE):
        try:
            with open(QA_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    # Merge saved with defaults
                    if "exact_questions" in saved and isinstance(saved["exact_questions"], dict):
                        default_data["exact_questions"].update(saved["exact_questions"])
                    if "patterns" in saved and isinstance(saved["patterns"], dict):
                        default_data["patterns"].update(saved["patterns"])
        except Exception as e:
            logging.warning(f"[QA Memory] Could not read {QA_FILE}: {e}")

    return default_data


def datetime_now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def save_qa_memory(data: Dict[str, Any]) -> bool:
    """Atomic write of QA memory to disk."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    data["last_updated"] = datetime_now_str()
    tmp_file = f"{QA_FILE}.tmp"
    try:
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_file, QA_FILE)
        return True
    except Exception as e:
        logging.warning(f"[QA Memory] Failed saving {QA_FILE}: {e}")
        return False


def save_learned_answer(question: str, answer: str, platform: Optional[str] = None) -> bool:
    """
    Persistently store a learned answer to disk so it is remembered forever
    across all platforms, runs, and wizard steps.
    """
    if not question or not answer or str(answer).strip() in ("", "SKIP"):
        return False

    ans_str = str(answer).strip()
    norm_q = _normalize_key(question)
    if not norm_q:
        return False

    memory = load_qa_memory()
    memory["exact_questions"][norm_q] = ans_str

    # Extract meaningful core sub-patterns (3+ words) for fuzzy matching
    words = norm_q.split()
    if len(words) >= 3:
        # e.g. "require sponsorship for employment", "current employer name"
        core_phrase = " ".join(words[:4])
        memory["patterns"][core_phrase] = ans_str

    saved = save_qa_memory(memory)
    if saved:
        logging.info(f"[QA Memory] 💾 Learned & Persisted: '{norm_q[:50]}...' -> '{ans_str}' (Platform: {platform or 'General'})")
    return saved


def match_best_option(answer: str, available_options: List[str]) -> Optional[str]:
    """Match a canonical answer to the closest available option in a UI list."""
    if not available_options:
        return None

    ans_low = answer.strip().lower()

    # 1. Exact match (case-insensitive)
    for opt in available_options:
        if opt.strip().lower() == ans_low:
            return opt

    # 2. Binary Yes/No alignment
    if ans_low in ("yes", "y", "true", "i agree", "agree"):
        for opt in available_options:
            o_low = opt.strip().lower()
            if o_low in ("yes", "i agree", "agree", "yes, i agree") or o_low.startswith("yes"):
                return opt

    if ans_low in ("no", "n", "false", "disagree"):
        for opt in available_options:
            o_low = opt.strip().lower()
            if o_low in ("no", "i disagree", "no, i do not") or o_low.startswith("no"):
                return opt

    # 3. Substring match
    for opt in available_options:
        o_low = opt.strip().lower()
        if ans_low in o_low or o_low in ans_low:
            return opt

    # 4. First word matching
    ans_first = ans_low.split()[0] if ans_low.split() else ""
    if len(ans_first) > 2:
        for opt in available_options:
            if opt.strip().lower().startswith(ans_first):
                return opt

    return None


def recall_answer(question: str, available_options: Optional[List[str]] = None) -> Optional[str]:
    """
    Retrieve previously learned answer for a given question using exact matching,
    normalized substring matching, and semantic pattern heuristics.
    """
    if not question:
        return None

    norm_q = _normalize_key(question)
    memory = load_qa_memory()
    exact_map = memory.get("exact_questions", {})
    patterns_map = memory.get("patterns", {})

    # 1. Direct exact normalized match
    if norm_q in exact_map:
        ans = exact_map[norm_q]
        if available_options:
            matched = match_best_option(ans, available_options)
            if matched:
                return matched
        return ans

    # 2. Substring matching against exact questions
    for saved_q, ans in exact_map.items():
        if len(saved_q) >= 12 and (saved_q in norm_q or norm_q in saved_q):
            if available_options:
                matched = match_best_option(ans, available_options)
                if matched:
                    return matched
            return ans

    # 3. Pattern / keyword phrase matching
    q_words = set(norm_q.split())
    for pattern, ans in patterns_map.items():
        p_norm = _normalize_key(pattern)
        if not p_norm:
            continue
        # Direct substring match
        if p_norm in norm_q:
            if available_options:
                matched = match_best_option(ans, available_options)
                if matched:
                    return matched
            return ans

        # Multi-word token inclusion (for natural variations like "preferred first name")
        p_words = p_norm.split()
        if len(p_words) >= 2:
            if all(w in q_words for w in p_words):
                if available_options:
                    matched = match_best_option(ans, available_options)
                    if matched:
                        return matched
                return ans

    return None
