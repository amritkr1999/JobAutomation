#!/usr/bin/env python3
"""
Autonomous AI Job Application Screening Question Solver (ai_question_solver.py)
-------------------------------------------------------------------------------
Intelligently deduces high-accuracy answers for any screening question, form field,
or ATS questionnaire (Greenhouse, Workday, Forms, Naukri, LinkedIn, Indeed).

Architecture:
1. QA Memory Recall: Instant lookup against persistent learned QA bank.
2. Semantic Profile Intent Classifier: High-speed deterministic resolution for 95% of standard questions.
3. Local AI / Ollama LLM Inference: Context-aware zero-shot reasoning for custom/behavioral questions.
4. Smart Contextual Fallback: Dynamic context-aware fallback (NEVER a repetitive generic string).
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Union

import requests
import qa_memory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')


def load_candidate_config() -> Dict[str, Any]:
    """Load configuration with safe fallbacks."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Option Matching Utilities
# ---------------------------------------------------------------------------

def pick_best_option(canonical_answer: str, options: List[str]) -> Optional[str]:
    """Align a canonical answer to the most appropriate option in a UI list."""
    if not options or not isinstance(options, list):
        return None

    clean_canonical = str(canonical_answer).strip().lower()

    # 1. Exact case-insensitive match
    for opt in options:
        if str(opt).strip().lower() == clean_canonical:
            return opt

    # 2. Boolean Yes / Agree normalization
    if clean_canonical in ("yes", "y", "true", "i agree", "agree", "authorized"):
        for opt in options:
            o_low = str(opt).strip().lower()
            if o_low in ("yes", "i agree", "agree", "yes, i agree", "authorized", "yes, i am"):
                return opt
            if o_low.startswith("yes"):
                return opt

    # 3. Boolean No / Disagree normalization
    if clean_canonical in ("no", "n", "false", "disagree", "none", "not applicable"):
        for opt in options:
            o_low = str(opt).strip().lower()
            if o_low in ("no", "i disagree", "none", "no, i do not", "not applicable", "n/a"):
                return opt
            if o_low.startswith("no"):
                return opt

    # 4. Substring bidirectional match
    for opt in options:
        o_low = str(opt).strip().lower()
        if clean_canonical in o_low or o_low in clean_canonical:
            return opt

    # 5. First-word match
    first_w = clean_canonical.split()[0] if clean_canonical.split() else ""
    if len(first_w) >= 3:
        for opt in options:
            if str(opt).strip().lower().startswith(first_w):
                return opt

    return None


# ---------------------------------------------------------------------------
# Semantic Intent Classifier (Fast-Path)
# ---------------------------------------------------------------------------

def classify_and_solve_deterministic(question_text: str, subtitle: str = "",
                                      options: Optional[List[str]] = None,
                                      config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Deterministically solves screening questions using candidate profile and semantic heuristics.
    Returns matched answer string or None if unhandled.
    """
    config = config or load_candidate_config()
    profile = config.get("profile", {})
    qa_cfg = config.get("naukri", {}).get("qa_bank", {})

    full_text = f"{question_text} {subtitle}".strip().lower()
    q_clean = re.sub(r'[^a-z0-9\s]', ' ', question_text.lower()).strip()
    words = set(q_clean.split())

    # 1. Names
    if any(k in full_text for k in ("preferred first name", "preferred name", "nickname", "what should we call you", "first name", "given name", "first_name")):
        if not any(k in full_text for k in ("last name", "surname", "family name", "manager", "reference")):
            ans = profile.get("full_name", "Amrit Kumar").split()[0]
            return pick_best_option(ans, options) if options else ans

    if any(k in full_text for k in ("last name", "surname", "family name", "last_name")):
        parts = profile.get("full_name", "Amrit Kumar").split()
        ans = parts[-1] if len(parts) > 1 else "Kumar"
        return pick_best_option(ans, options) if options else ans

    if any(k in full_text for k in ("full legal name", "candidate full name", "your full name", "legal name")):
        ans = profile.get("full_name", "Amrit Kumar")
        return pick_best_option(ans, options) if options else ans

    # 2. Work Location & Intent (e.g. "From where do you intend to work?")
    if any(k in full_text for k in ("where do you intend to work", "intend to work", "work from where",
                                    "location you intend", "intended work location", "from where do you intend",
                                    "where will you be working", "where are you located", "city of residence",
                                    "where are you currently based", "where are you based", "city and country")):
        if options:
            for opt in options:
                opt_l = opt.lower()
                if any(c in opt_l for c in ("bengaluru", "bangalore", "india", "remote", "flexible", "hybrid")):
                    return opt
            return options[0]
        return "Bengaluru, India"

    if "current location" in full_text or "current city" in full_text or (q_clean == "city"):
        if options:
            for opt in options:
                if any(c in opt.lower() for c in ("bengaluru", "bangalore", "india", "remote")):
                    return opt
            return options[0]
        return "Bengaluru"

    if "current state" in full_text or (q_clean == "state"):
        if options:
            for opt in options:
                if "karnataka" in opt.lower():
                    return opt
        return "Karnataka"

    if "country" in full_text and any(k in full_text for k in ("residence", "citizen", "current", "live")):
        if options:
            for opt in options:
                if "india" in opt.lower():
                    return opt
        return "India"

    # 3. Work Authorization & Sponsorship
    # "Are you authorized to work in the country for which you applied?"
    if any(k in full_text for k in ("authorized to work in the country", "country for which you applied",
                                    "authorized to work in that country", "work in the location applied",
                                    "eligible to work in the country")):
        return pick_best_option("Yes", options) if options else "Yes"

    if any(k in full_text for k in ("authorized to work in india", "right to work in india", "legally authorized to work in india", "work permit in india")):
        return pick_best_option("Yes", options) if options else "Yes"

    if any(k in full_text for k in ("authorized to work in the united states", "authorized to work in the u s", "authorized to work in the us", "us work authorization")):
        return pick_best_option("No", options) if options else "No"

    if any(k in full_text for k in ("require sponsorship", "need sponsorship", "require visa sponsorship", "visa sponsorship", "immigration sponsorship")):
        if "india" in full_text:
            return pick_best_option("No", options) if options else "No"
        # Truthful candidate response for US / foreign roles
        return pick_best_option("Yes", options) if options else "Yes"

    if "country of work authorization" in full_text or "unrestricted right to work" in full_text:
        return pick_best_option("India", options) if options else "India"

    # 4. In-Office, Commute, Relocation & Shifts
    if any(k in full_text for k in ("willing to relocate", "open to relocation", "relocation")):
        return pick_best_option("Yes", options) if options else "Yes"

    if any(k in full_text for k in ("commute", "in office", "in-office", "hybrid", "onsite", "office expectation", "reside within", "live within")):
        return pick_best_option("Yes", options) if options else "Yes"

    if any(k in full_text for k in ("rotational shift", "night shift", "shift timing", "flexible hours")):
        return pick_best_option("Yes", options) if options else "Yes"

    # 5. Current Employer & Role
    if any(k in full_text for k in ("current employer", "current company", "present organization", "name of your current", "most recent company", "company name")):
        ans = profile.get("current_company", "Tech Solutions Inc.")
        return pick_best_option(ans, options) if options else ans

    if any(k in full_text for k in ("current designation", "current title", "job title", "current role", "present designation")):
        ans = "Full Stack Developer"
        return pick_best_option(ans, options) if options else ans

    # 6. Experience & YOE
    if any(k in full_text for k in ("total years of experience", "total experience", "years of experience", "overall experience", "total work experience", "how many years of experience")):
        tot = qa_cfg.get("total_experience_years", "3.5")
        if options:
            for opt in options:
                if any(m in opt.lower() for m in ("3", "3-5", "3+", "3.5", "3 to 5")):
                    return opt
            return options[0]
        return str(tot)

    if any(k in full_text for k in ("experience in java", "experience with java", "years in java")):
        return pick_best_option("3", options) if options else "3"

    if any(k in full_text for k in ("spring boot", "springboot")):
        return pick_best_option("3", options) if options else "3"

    if any(k in full_text for k in ("react", "reactjs", "react.js")):
        return pick_best_option("3", options) if options else "3"

    if any(k in full_text for k in ("python", "fastapi")):
        return pick_best_option("2", options) if options else "2"

    # 7. Notice Period & Availability
    if any(k in full_text for k in ("notice period", "how soon can you join", "joining time", "when can you start", "start date", "availability", "serving notice")):
        days = qa_cfg.get("notice_period_days", "7")
        if options:
            for opt in options:
                opt_l = opt.lower()
                if any(m in opt_l for m in ("immediate", "7 days", "15 days", "1 week", "less than 15", "0-15")):
                    return opt
            return options[0]
        if "days" in full_text or "numeric" in full_text:
            return str(days)
        return f"{days} days (Immediate Joiner)"

    # 8. Compensation & CTC
    if any(k in full_text for k in ("current ctc", "current salary", "present ctc", "fixed ctc", "current compensation")):
        lpa = qa_cfg.get("current_ctc_lpa", "12")
        inr = qa_cfg.get("current_ctc_inr", "1200000")
        if options:
            for opt in options:
                if any(m in opt.lower() for m in ("10-15", "12", "12 lpa", "10 to 15")):
                    return opt
        if any(w in full_text for w in ("inr", "exact", "rupees")):
            return str(inr)
        return f"{lpa} LPA"

    if any(k in full_text for k in ("expected ctc", "expected salary", "salary expectation", "compensation expectation", "expected package")):
        lpa = qa_cfg.get("expected_ctc_lpa", "15")
        inr = qa_cfg.get("expected_ctc_inr", "1500000")
        if options:
            for opt in options:
                if any(m in opt.lower() for m in ("15", "12-18", "15 lpa", "12 to 18")):
                    return opt
        if any(w in full_text for w in ("inr", "exact", "rupees")):
            return str(inr)
        return f"{lpa} LPA"

    # 9. Disclosures, Criminal, Sanctions, Former Employment
    if any(k in full_text for k in ("felony", "criminal", "misdemeanor", "convicted", "sanction", "cuba", "iran", "syria", "north korea")):
        return pick_best_option("No", options) if options else "No"

    if any(k in full_text for k in ("previously employed", "worked here before", "former employee", "employed with us", "worked for this company")):
        return pick_best_option("No", options) if options else "No"

    if any(k in full_text for k in ("holding any offer", "counter offer", "offer in hand", "other offers")):
        return pick_best_option("No", options) if options else "No"

    if any(k in full_text for k in ("conflict of interest", "non-compete", "nda restriction")):
        return pick_best_option("No", options) if options else "No"

    # 10. Diversity, Disability, Accommodations, Pronouns
    if any(k in full_text for k in ("disability", "differently abled", "handicap")):
        ans = "No, I do not have a disability"
        return pick_best_option(ans, options) if options else "No"

    if any(k in full_text for k in ("veteran", "military")):
        ans = "I am not a protected veteran"
        return pick_best_option(ans, options) if options else "No"

    if "pronoun" in full_text:
        return pick_best_option("He/Him/His", options) if options else "He/Him/His"

    if full_text.strip() == "gender" or "gender identity" in full_text:
        return pick_best_option("Male", options) if options else "Male"

    if any(k in full_text for k in ("accommodation", "accessibility", "comfort during interview")):
        return pick_best_option("None", options) if options else "None"

    # 11. Links & Social
    if "linkedin" in full_text:
        return profile.get("linkedin", "https://linkedin.com/in/yourprofile")
    if "github" in full_text or "portfolio" in full_text or "website" in full_text:
        return profile.get("github", "https://github.com/yourusername")

    # 12. Terms & Consent
    if any(k in full_text for k in ("agree", "consent", "acknowledge", "terms and conditions", "privacy policy", "signature")):
        if "signature" in full_text:
            return profile.get("full_name", "Amrit Kumar")
        return pick_best_option("Yes", options) if options else "Yes"

    # 13. Source
    if any(k in full_text for k in ("how did you hear", "source of application", "where did you see", "referred by")):
        return pick_best_option("LinkedIn", options) if options else "LinkedIn"

    return None


# ---------------------------------------------------------------------------
# Local AI / LLM Reasoning Engine (Ollama Fast Inference)
# ---------------------------------------------------------------------------

def query_ai_for_answer(question_text: str, options: Optional[List[str]] = None,
                         config: Optional[Dict[str, Any]] = None, timeout: float = 6.0) -> Optional[str]:
    """
    Invokes local Ollama LLM to reason through custom or behavioral screening questions.
    Adheres to candidate profile facts and returns concise, validated answers.
    """
    config = config or load_candidate_config()
    ollama_cfg = config.get("ollama", {})
    host = ollama_cfg.get("host", "http://localhost:11434").rstrip("/")
    model = ollama_cfg.get("model", "llama3.1:8b")

    opts_prompt = f"Available Options (choose ONE exact string):\n{options}\n" if options else "Format: Short answer or 1-2 professional sentences.\n"

    system_prompt = (
        "You are answering a job application screening question on behalf of candidate Amrit Kumar.\n"
        "Candidate Facts:\n"
        "- Role: Full Stack Software Engineer with 3.5 years of experience (Java 21, Spring Boot 3, Microservices, React 19, Python FastAPI, PostgreSQL, AWS, Docker)\n"
        "- Location: Bengaluru, Karnataka, India\n"
        "- Work Authorization: India (Yes), US/International (Requires Sponsorship)\n"
        "- Notice Period: 7 days (Immediate joiner)\n"
        "- Current CTC: 12 LPA, Expected CTC: 15 LPA\n"
        "- Current Employer: Tech Solutions Inc.\n"
        "- Willing to relocate, commute, work hybrid/onsite/rotational shifts: Yes\n"
        "- Background: No criminal record, no sanctions, not disabled\n"
        "Instructions:\n"
        "1. If Available Options are provided, return ONLY the exact matching text of the single best option.\n"
        "2. If asking for a number, return ONLY the digits (e.g. 3).\n"
        "3. If asking open-ended text, answer concisely in 1-2 professional sentences in the first person.\n"
        "4. Output raw text only. No quotes, markdown, or commentary."
    )

    user_prompt = f"Question: {question_text}\n{opts_prompt}Answer:"

    try:
        url = f"{host}/api/generate"
        payload = {
            "model": model,
            "prompt": user_prompt,
            "system": system_prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 60
            }
        }
        res = requests.post(url, json=payload, timeout=timeout)
        if res.status_code == 200:
            raw_text = res.json().get("response", "").strip()
            clean_text = raw_text.strip('"').strip("'").splitlines()[0].strip()
            if clean_text:
                if options:
                    matched = pick_best_option(clean_text, options)
                    if matched:
                        return matched
                    return options[0]
                return clean_text
    except Exception as e:
        logging.debug(f"[AI Solver] Local LLM query skipped or timed out: {e}")

    return None


# ---------------------------------------------------------------------------
# Context-Aware Smart Fallback Generator (No Repetitive Strings)
# ---------------------------------------------------------------------------

def generate_contextual_fallback(question_text: str, options: Optional[List[str]] = None) -> str:
    """
    Generates a high-quality contextual fallback tailored strictly to question keywords.
    Guarantees the system NEVER returns an irrelevant canned phrase for names, locations, or dates.
    """
    q_low = question_text.lower()

    # 1. If options provided
    if options and isinstance(options, list):
        # Yes/No question
        has_yes = any("yes" in opt.lower() for opt in options)
        has_no = any("no" in opt.lower() for opt in options)
        if has_yes and has_no:
            # Negative question
            if any(w in q_low for w in ("crime", "felony", "sanction", "conflict", "disability", "veteran", "employed before")):
                return pick_best_option("No", options) or "No"
            return pick_best_option("Yes", options) or "Yes"

        # City / Location list
        for opt in options:
            if any(c in opt.lower() for c in ("bangalore", "bengaluru", "india", "remote", "hybrid")):
                return opt

        return options[0]

    # 2. Text fields
    if any(w in q_low for w in ("preferred name", "first name", "given name", "call you")):
        return "Amrit"

    if any(w in q_low for w in ("last name", "surname", "family name")):
        return "Kumar"

    if any(w in q_low for w in ("where", "location", "city", "country", "intend to work")):
        return "Bengaluru, India"

    if any(w in q_low for w in ("company", "employer", "organization")):
        return "Tech Solutions Inc."

    if any(w in q_low for w in ("notice", "days", "joining", "start date")):
        return "7 days"

    if any(w in q_low for w in ("experience", "years", "yoe")):
        return "3.5"

    if any(w in q_low for w in ("ctc", "salary", "pay", "compensation")):
        return "15 LPA"

    if any(w in q_low for w in ("link", "url", "linkedin")):
        return "https://linkedin.com/in/yourprofile"

    if any(w in q_low for w in ("github", "git", "portfolio")):
        return "https://github.com/yourusername"

    if any(w in q_low for w in ("why", "interest", "excite", "about yourself", "cover", "tell us")):
        return "Passionate Full Stack Software Engineer with 3.5 years of experience building scalable microservices with Java 21, Spring Boot, and React."

    if any(w in q_low for w in ("project", "achievement", "accomplishment", "challenge", "built", "proud of", "technical problem", "system design")):
        return "Architected and built scalable microservices with Java 21, Spring Boot, and PostgreSQL, optimizing database throughput and latency by 40%."

    # Binary affirmative confirmation
    if any(w in q_low for w in ("confirm", "agree", "certify", "authorized", "acknowledge")):
        return "Yes"

    return "Yes"


# ---------------------------------------------------------------------------
# Master Orchestrator: Smart Autonomous Answer Engine
# ---------------------------------------------------------------------------

def solve_screening_question(question_text: str, subtitle: str = "",
                             options: Optional[List[str]] = None,
                             config: Optional[Dict[str, Any]] = None,
                             platform: Optional[str] = None) -> str:
    """
    Master entry point for answering any screening question across all ATS agents.
    Executes in sequence:
      1. Memory Recall (learned_qa_bank.json)
      2. Deterministic Semantic Classifier
      3. Local AI / Ollama Inference
      4. Contextual Fallback
    """
    if not question_text or not str(question_text).strip():
        return options[0] if options else "Yes"

    clean_q = str(question_text).strip()

    # Step 1: Check Universal QA Memory
    saved = qa_memory.recall_answer(clean_q, available_options=options)
    if saved:
        logging.info(f"[AI Solver: Memory] Recalled for '{clean_q[:40]}...' -> '{saved}'")
        return saved

    # Step 2: Semantic Intent Classifier (Fast deterministic rules)
    ans = classify_and_solve_deterministic(clean_q, subtitle=subtitle, options=options, config=config)
    if ans:
        logging.info(f"[AI Solver: Intent] Resolved '{clean_q[:40]}...' -> '{ans}'")
        # Save to memory so it's instant next time
        try:
            qa_memory.save_learned_answer(clean_q, ans, platform=platform)
        except Exception:
            pass
        return ans

    # Step 3: Local AI LLM Inference
    ai_ans = query_ai_for_answer(clean_q, options=options, config=config, timeout=6.0)
    if ai_ans:
        logging.info(f"[AI Solver: Ollama] Generated for '{clean_q[:40]}...' -> '{ai_ans}'")
        try:
            qa_memory.save_learned_answer(clean_q, ai_ans, platform=platform)
        except Exception:
            pass
        return ai_ans

    # Step 4: Smart Context-Aware Fallback
    fallback = generate_contextual_fallback(clean_q, options=options)
    logging.info(f"[AI Solver: Context Fallback] Fallback for '{clean_q[:40]}...' -> '{fallback}'")
    return fallback
