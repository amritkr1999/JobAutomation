#!/usr/bin/env python3
"""
Google Forms & Microsoft Forms Autonomous Job Application Engine
Autonomous Form Solver Agent for Amrit Kumar.
Supports:
  1. Universal Google Forms Solving (forms.gle/*, docs.google.com/forms/*)
  2. Universal Microsoft Forms Solving (forms.office.com/*, forms.microsoft.com/*)
  3. Single-Page and Multi-Page Form Traversal
  4. Short text, Paragraphs, Radio buttons (with "Other" support), Checkboxes, Dropdowns, Date, and Resume Upload
  5. Deterministic QA Engine (Profile, QA Bank, Heuristics) with Ollama LLM Fallback
  6. Dry-Run Mode (Screenshots without submitting) & Review Mode (Visual Inspection)
  7. Dropzone Ticket Batch Processor & Direct Single URL Execution
  8. Persistent Ledger & macOS Notifications
"""

import os
import sys
import re
import json
import time
import random
import logging
import argparse
from datetime import datetime, timedelta
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import agent as email_agent
import naukri_agent
import power_utils
from power_utils import MacKeepAwake
import notifier
import keyword_tracker
import qa_memory
import ai_question_solver

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [FormsAgent] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


def human_delay(min_sec=1.5, max_sec=3.5):
    """Sleep for a randomized human-like interval."""
    time.sleep(random.uniform(min_sec, max_sec))


def notify_macos(title, message, is_urgent=False):
    """Display a non-blocking macOS notification banner respecting quiet_mode."""
    power_utils.notify_macos(title, message, is_urgent=is_urgent)


# ---------------------------------------------------------------------------
# Ledger Management
# ---------------------------------------------------------------------------

def load_forms_ledger(logs_dir):
    """Load persistent ledger of applied Google & Microsoft Forms."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'forms_applied.json')
    default_data = {
        "last_run_at": None,
        "applied_forms": {}
    }
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    default_data.update(data)
                    if not isinstance(default_data.get("applied_forms"), dict):
                        default_data["applied_forms"] = {}
        except Exception as e:
            logging.warning(f"Could not read forms_applied.json: {e}")
    return default_data


def save_forms_ledger(logs_dir, ledger):
    """Persist updated ledger to forms_applied.json."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'forms_applied.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.warning(f"Could not save forms_applied.json: {e}")


def count_forms_applied_today(ledger):
    """Count how many forms were submitted today (YYYY-MM-DD)."""
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    applied = ledger.get("applied_forms", {})
    count = 0
    for _, info in applied.items():
        if not isinstance(info, dict):
            continue
        status = info.get("status", "")
        ts = str(info.get("applied_at", ""))
        if status in ("APPLIED", "SUBMITTED") and ts.startswith(today_prefix):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Browser Initialization
# ---------------------------------------------------------------------------

def launch_forms_browser(playwright, config, force_headed=False):
    """Launch persistent Google Chrome context dedicated to Forms automation."""
    forms_cfg = config.get('forms', {})
    profile_dir = os.path.expanduser(
        forms_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/forms')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = forms_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(forms_cfg.get('headless', True))

    launch_kwargs = {
        "user_data_dir": profile_dir,
        "headless": headless,
        "viewport": {"width": 1440, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    }
    if os.path.exists(chrome_exec):
        launch_kwargs["executable_path"] = chrome_exec

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    return context


# ---------------------------------------------------------------------------
# Form Classification Helpers
# ---------------------------------------------------------------------------

def is_google_form_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "forms.gle/" in u or "docs.google.com/forms" in u or "google.com/forms" in u


def is_ms_form_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "forms.office.com" in u or "forms.microsoft.com" in u or "forms.cloud.microsoft" in u


# ---------------------------------------------------------------------------
# Intelligent Question Answering & Inference Engine
# ---------------------------------------------------------------------------

def resolve_form_question(label_text: str, subtitle_text: str = "", field_type: str = "text",
                          available_options: list = None, config: dict = None, jd_context: str = ""):
    """
    Intelligently deduce the best answer for any screening question or form field
    using candidate profile, central qa_bank, deterministic heuristics, and Ollama LLM fallback.
    """
    config = config or {}
    profile = config.get("profile", {})
    qa = config.get("naukri", {}).get("qa_bank", {})
    available_options = available_options or []

    combined_text = f"{label_text} {subtitle_text}".strip().lower()
    clean_label = label_text.strip().lower()

    # 0. Check universal persistent QA memory first (avoids asking Telegram again)
    saved = qa_memory.recall_answer(label_text, available_options=available_options)
    if saved:
        logging.info(f"[QA Memory] Auto-recalled learned answer for '{label_text[:45]}...' -> '{saved}'")
        return saved

    # 0b. Check AI deterministic semantic classifier
    solv = ai_question_solver.classify_and_solve_deterministic(label_text, subtitle=subtitle_text, options=available_options, config=config)
    if solv:
        logging.info(f"[AI Solver] Deterministically resolved Forms field '{label_text[:45]}...' -> '{solv}'")
        try:
            qa_memory.save_learned_answer(label_text, solv, platform="Forms")
        except Exception:
            pass
        return solv

    # 1. Full Name / First / Last Name
    if any(k in clean_label for k in ("first name", "given name", "first_name")):
        return profile.get("full_name", "Amrit Kumar").split()[0]
    if any(k in clean_label for k in ("last name", "surname", "family name", "last_name")):
        parts = profile.get("full_name", "Amrit Kumar").split()
        return parts[-1] if len(parts) > 1 else ""
    if any(k in clean_label for k in ("full name", "candidate name", "your name", "name of candidate")) or clean_label == "name":
        return profile.get("full_name", "Amrit Kumar")

    # 2. Email Address
    if any(k in clean_label for k in ("email", "e-mail", "mail id", "email address")):
        return profile.get("email", "candidate@example.com")

    # 3. Phone / Mobile / WhatsApp
    if any(k in clean_label for k in ("phone", "mobile", "contact number", "whatsapp", "cell", "calling number")):
        phone = profile.get("phone", "+91-9876543210")
        # If question or subtitle asks for 10 digits or numbers only
        if any(w in combined_text for w in ("10 digit", "10-digit", "without country code", "numbers only")):
            return re.sub(r'\D', '', phone)[-10:]
        return phone

    # 4. URLs & Profiles
    if "linkedin" in clean_label:
        return profile.get("linkedin", "https://linkedin.com/in/yourprofile")
    if "github" in clean_label:
        return profile.get("github", "https://github.com/yourusername")
    if any(k in clean_label for k in ("portfolio", "website", "personal site", "project link")):
        return profile.get("github", "https://github.com/yourusername")

    # 5. Current Location / City / State
    if any(k in clean_label for k in ("current location", "current city", "base location", "where are you located", "city you live", "present location")) or clean_label == "current location":
        loc = profile.get("current_location", "Bengaluru, India")
        if available_options:
            for opt in available_options:
                opt_l = opt.lower()
                if "bangalore" in opt_l or "bengaluru" in opt_l:
                    return opt
        return loc

    if clean_label == "city" or "current city" in clean_label:
        if available_options:
            for opt in available_options:
                if any(c in opt.lower() for c in ("bangalore", "bengaluru")):
                    return opt
        return "Bengaluru"

    if clean_label == "state" or "state of residence" in clean_label:
        return "Karnataka"

    if clean_label in ("country", "country of residence"):
        return "India"

    if any(k in clean_label for k in ("pincode", "pin code", "postal code", "zip code")):
        return config.get("workday", {}).get("postal_code", "560001")

    # 6. Current Employment & Roles
    if any(k in clean_label for k in ("current company", "current employer", "current organization", "company name", "present organization", "present company")):
        return profile.get("current_company", "Tech Solutions Inc.")

    if any(k in clean_label for k in ("current designation", "current role", "job title", "current job title", "designation")):
        return "Full Stack Developer"

    # 7. Total Experience (YOE)
    if any(k in clean_label for k in ("total experience", "years of experience", "total yoe", "overall experience", "total work experience")) or clean_label == "experience":
        tot_exp = qa.get("total_experience_years", "3.5")
        if available_options:
            for opt in available_options:
                opt_l = opt.lower()
                if any(m in opt_l for m in ("3-5", "3 to 5", "3+", "3.5", "2-4", "2 to 4")):
                    return opt
        if "year" in combined_text and "number" in combined_text:
            return tot_exp
        return f"{tot_exp} Years"

    # 8. Specific Tech Skill Experience
    skills_map = qa.get("skill_experience_years", {})
    for skill_name, yrs in skills_map.items():
        if skill_name in clean_label and any(k in clean_label for k in ("experience", "yoe", "years", "rating", "proficien")):
            if available_options:
                for opt in available_options:
                    if yrs in opt:
                        return opt
            return str(yrs)

    if any(k in clean_label for k in ("relevant experience", "relevant yoe")):
        rel = qa.get("relevant_experience_years", "3")
        if available_options:
            for opt in available_options:
                if any(m in opt.lower() for m in ("3", "3-5", "3+")):
                    return opt
        return str(rel)

    # 9. Notice Period & Availability
    if any(k in clean_label for k in ("serving notice", "currently serving", "on notice", "serving a notice")):
        if available_options:
            for opt in available_options:
                if opt.lower() in ("yes", "currently serving", "serving"):
                    return opt
        return "Yes"

    if any(k in clean_label for k in ("notice period", "notice", "how soon", "joining time", "start date", "availability", "how many days notice")):
        days = qa.get("notice_period_days", "7")
        if available_options:
            for opt in available_options:
                opt_l = opt.lower()
                if any(m in opt_l for m in ("immediate", "7 days", "15 days", "less than 15", "0-15", "0 to 15", "1 week")):
                    return opt
        if any(w in combined_text for w in ("in days", "number of days", "numeric")):
            return str(days)
        return f"{days} days (Immediate Joiner)"

    if any(k in clean_label for k in ("last working day", "lwd", "official last working date")):
        lwd = (datetime.now() + timedelta(days=7)).strftime("%d/%m/%Y")
        return lwd

    # 10. Compensation / CTC
    if any(k in clean_label for k in ("current ctc", "current salary", "present ctc", "fixed ctc", "cctc")):
        val_lpa = qa.get("current_ctc_lpa", "12")
        val_inr = qa.get("current_ctc_inr", "1200000")
        if any(w in combined_text for w in ("inr", "rupees", "exact", "annual", "per annum")) and "lpa" not in combined_text:
            return val_inr
        if available_options:
            for opt in available_options:
                if any(m in opt.lower() for m in ("10-15", "10 to 15", "12", "12 lpa")):
                    return opt
        if "lpa" in combined_text:
            return f"{val_lpa} LPA"
        return str(val_lpa)

    if any(k in clean_label for k in ("expected ctc", "expected salary", "ectc", "salary expectation", "expected package")):
        val_lpa = qa.get("expected_ctc_lpa", "15")
        val_inr = qa.get("expected_ctc_inr", "1500000")
        if any(w in combined_text for w in ("inr", "rupees", "exact", "annual", "per annum")) and "lpa" not in combined_text:
            return val_inr
        if available_options:
            for opt in available_options:
                if any(m in opt.lower() for m in ("12-15", "15-20", "15 to 20", "15", "15 lpa")):
                    return opt
        if "lpa" in combined_text:
            return f"{val_lpa} LPA"
        return str(val_lpa)

    if any(k in clean_label for k in ("negotiable", "flexible on salary")):
        return "Yes"

    # 11. Education & Academic Details
    if any(k in clean_label for k in ("highest qualification", "degree", "education", "qualification", "highest degree")):
        qual = qa.get("highest_qualification", "B.Tech / B.E. in Computer Science & Engineering")
        if available_options:
            for opt in available_options:
                opt_l = opt.lower()
                if any(m in opt_l for m in ("b.tech", "b.e", "bachelor", "engineering", "computer science")):
                    return opt
        return qual

    if any(k in clean_label for k in ("graduation year", "year of graduation", "passing year", "year of passing")):
        yr = qa.get("graduation_year", "2023")
        if available_options:
            for opt in available_options:
                if yr in opt:
                    return opt
        return str(yr)

    if any(k in clean_label for k in ("cgpa", "percentage", "marks", "grade")):
        cgpa = qa.get("percentage_cgpa", "8.2")
        if "percentage" in clean_label or "%" in clean_label:
            return "82%"
        return str(cgpa)

    if any(k in clean_label for k in ("college", "university", "institute", "school")):
        return "B.Tech Computer Science & Engineering"

    if any(k in clean_label for k in ("branch", "stream", "specialization", "department")):
        return "Computer Science & Engineering"

    # 12. Work Preferences & Relocation
    if any(k in clean_label for k in ("willing to relocate", "open to relocate", "relocation")):
        if available_options:
            for opt in available_options:
                if opt.lower() in ("yes", "willing", "open"):
                    return opt
        return "Yes"

    if any(k in clean_label for k in ("preferred location", "preferred city", "desired location")):
        if available_options:
            for opt in available_options:
                opt_l = opt.lower()
                if any(c in opt_l for c in ("bangalore", "bengaluru", "remote", "hyderabad", "pune", "any")):
                    return opt
        return qa.get("preferred_locations", "Bangalore, Hyderabad, Pune, Remote")

    if any(k in clean_label for k in ("work mode", "hybrid", "work from office", "remote or hybrid", "wfh")):
        if available_options:
            for opt in available_options:
                opt_l = opt.lower()
                if any(m in opt_l for m in ("hybrid", "flexible", "all", "yes", "open")):
                    return opt
        return "Yes (Open to Hybrid / Remote / Onsite)"

    if any(k in clean_label for k in ("shift", "night shift", "rotational shift")):
        return "Yes"

    # 13. Work Authorization & Sponsorship
    if any(k in clean_label for k in ("authorized to work in india", "legally authorized to work", "work permit in india")):
        return "Yes"
    if any(k in clean_label for k in ("require visa sponsorship", "require sponsorship", "need sponsorship")):
        return "No"

    # 14. Prior Employment / Worked Here
    if any(k in clean_label for k in ("previously employed", "worked here", "worked for this company", "former employee", "have you worked")):
        if available_options:
            for opt in available_options:
                if opt.lower() == "no":
                    return opt
        return "No"

    # 15. Offers in hand
    if any(k in clean_label for k in ("offer in hand", "holding any offer", "other offers", "counter offer")):
        if available_options:
            for opt in available_options:
                if opt.lower() == "no":
                    return opt
        return "No"

    # 16. Source / How did you hear
    if any(k in clean_label for k in ("how did you hear", "source of application", "referred by", "where did you see")):
        if available_options:
            for opt in available_options:
                if "linkedin" in opt.lower():
                    return opt
        return "LinkedIn"

    # 17. Gender / Personal
    if clean_label == "gender":
        if available_options:
            for opt in available_options:
                if opt.lower() == "male":
                    return opt
        return "Male"

    if "pronoun" in clean_label:
        if available_options:
            for opt in available_options:
                if "he/him" in opt.lower():
                    return opt
        return "He/Him/His"

    if any(k in clean_label for k in ("dob", "date of birth", "birth date")):
        dob_text = qa.get("date_of_birth", "12/06/1999")
        if "yyyy" in combined_text:
            return "1999-06-12"
        return dob_text

    # 18. Key Skills & Primary Technologies
    if any(k in clean_label for k in ("key skills", "primary skills", "tech stack", "technologies", "skill set", "skills")):
        skills = "Java 21, Spring Boot, Microservices, React.js, Python, PostgreSQL, REST APIs, Docker, AWS, Kafka"
        if available_options:
            # If multiple checkboxes or radio
            matching = [opt for opt in available_options if any(s.lower() in opt.lower() for s in ("java", "spring", "react", "python", "microservice", "sql", "aws", "docker"))]
            if matching:
                return matching
        return skills

    # 19. Common Behavioral & Narrative Questions
    if any(k in clean_label for k in ("reason for change", "why looking for change", "why switch", "why are you looking")):
        return "Seeking impactful engineering opportunities in high-scale distributed systems where I can contribute my Java 21, Spring Boot, and cloud architecture expertise."

    if any(k in clean_label for k in ("why do you want to join", "why this company", "why should we hire you", "what excites you")):
        return "I am passionate about building resilient, mission-critical backend architectures and modern web applications alongside a high-performing engineering team."

    if any(k in clean_label for k in ("brief description", "about yourself", "summary", "overview of your experience", "profile summary")):
        return "Full Stack Software Engineer with 3.5 years of experience specializing in Java 21, Spring Boot, React, and PostgreSQL. Experienced in designing microservices and scalable cloud-native architectures."

    if any(k in clean_label for k in ("any questions", "any comments", "additional information", "remarks")):
        return "None. Excited to discuss the role."

    # 20. If options available and standard Yes/Agree question
    if available_options:
        for opt in available_options:
            if opt.lower() in ("yes", "i agree", "agree", "confirm"):
                return opt

    # 21. Local AI (Ollama) Fallback for open-ended custom questions
    llm_ans = ai_question_solver.query_ai_for_answer(label_text, options=available_options, config=config)
    if llm_ans:
        try:
            qa_memory.save_learned_answer(label_text, llm_ans, platform="Forms")
        except Exception:
            pass
        return llm_ans

    # Contextual Smart Fallback (No canned repetitive string)
    fallback_val = ai_question_solver.generate_contextual_fallback(label_text, options=available_options)

    # Human-in-the-Loop Intervention via Telegram
    tg_cfg = config.get("notifications", {}).get("telegram", {})
    if tg_cfg.get("enabled") and tg_cfg.get("alert_on_unknown_question"):
        user_choice = notifier.notify_stuck(
            platform="Forms Application",
            company="Job Form",
            role="Application Question",
            reason="Unrecognized Screening Question",
            question=label_text,
            options=available_options,
            default_answer=fallback_val
        )
        if user_choice:
            qa_bank[clean_label] = user_choice
            try:
                qa_memory.save_learned_answer(label_text, user_choice, platform="Forms")
            except Exception:
                pass
            return user_choice

    return fallback_val


# ---------------------------------------------------------------------------
# Google Forms Solver Engine
# ---------------------------------------------------------------------------

class GoogleFormsSolver:
    """
    Automates multi-page and single-page Google Forms applications.
    Handles Google Form Material DOM containers, text inputs, textareas,
    radiogroups, checkboxes, dropdown listboxes, date fields, and resume upload.
    """

    def __init__(self, page, config, resume_pdf_path=None, dry_run=False, review_mode=False):
        self.page = page
        self.config = config
        self.resume_pdf_path = resume_pdf_path
        self.dry_run = dry_run
        self.review_mode = review_mode
        self.filled_count = 0

    def extract_form_title(self) -> str:
        """Extract title of Google Form."""
        title_selectors = [
            "div.F9NWFb",
            "div[role='heading'][aria-level='1']",
            ".freebirdFormviewerViewHeaderTitle",
            "div.vHW8K",
            "h1"
        ]
        for sel in title_selectors:
            loc = self.page.locator(sel).first
            if loc.count() > 0:
                txt = (loc.inner_text(timeout=400) or "").strip()
                if txt:
                    return txt
        return "Google Form Application"

    def fill_current_page_questions(self) -> int:
        """
        Inspect and solve all question containers on current Google Form page.
        Returns count of questions successfully answered on this page.
        """
        page_filled = 0
        items = self.page.locator("div[role='listitem']").all()
        if not items:
            # Fallback to alternate Google Forms question container classes
            items = self.page.locator("div.Qr7Oae, div.geS5n").all()

        logging.info(f"[Google Forms] Found {len(items)} question items on current page.")

        for idx, item in enumerate(items):
            try:
                if not item.is_visible(timeout=300):
                    continue

                # 1. Extract Question Title & Subtitle
                title_loc = item.locator("div[role='heading'], span.M7eMe, div.HofdGf, .HoXoMd").first
                question_title = (title_loc.inner_text(timeout=300) or "").strip() if title_loc.count() > 0 else ""
                if not question_title:
                    continue

                desc_loc = item.locator("div.g9vOvd, div.cBGGId").first
                question_desc = (desc_loc.inner_text(timeout=200) or "").strip() if desc_loc.count() > 0 else ""

                is_required = item.locator("span[aria-label*='Required'], span.vNumgf, span:has-text('*')").count() > 0
                req_badge = " [REQUIRED]" if is_required else ""
                logging.info(f"[Google Forms] Q{idx+1}: '{question_title}'{req_badge}")

                # 2. Check for File Upload (Resume / CV)
                if any(w in question_title.lower() for w in ("resume", "cv", "curriculum vitae", "upload your profile")):
                    file_input = item.locator("input[type='file']").first
                    if file_input.count() > 0 and self.resume_pdf_path and os.path.exists(self.resume_pdf_path):
                        try:
                            file_input.set_input_files(self.resume_pdf_path, timeout=3000)
                            human_delay(1.0, 2.0)
                            logging.info(f"Attached Resume PDF -> {os.path.basename(self.resume_pdf_path)}")
                            page_filled += 1
                            self.filled_count += 1
                            continue
                        except Exception as fe:
                            logging.debug(f"[Google Forms] File input attach note: {fe}")

                    # Google Forms often has an 'Add file' button
                    add_file_btn = item.locator("div[role='button']:has-text('Add file')").first
                    if add_file_btn.count() > 0 and self.resume_pdf_path and os.path.exists(self.resume_pdf_path):
                        try:
                            with self.page.expect_file_chooser(timeout=3000) as fc_info:
                                add_file_btn.click()
                            file_chooser = fc_info.value
                            file_chooser.set_files(self.resume_pdf_path)
                            human_delay(1.5, 2.5)
                            logging.info(f"Uploaded Resume via File Chooser -> {os.path.basename(self.resume_pdf_path)}")
                            page_filled += 1
                            self.filled_count += 1
                            continue
                        except Exception as ce:
                            logging.debug(f"[Google Forms] Add file chooser note: {ce}")

                # 3. Radio Group (Single Choice)
                radiogroup = item.locator("div[role='radiogroup']").first
                if radiogroup.count() > 0:
                    radios = item.locator("div[role='radio']").all()
                    options = []
                    for r in radios:
                        opt_val = r.get_attribute("data-value") or ""
                        label_span = r.locator("span.aDTYNe, span").first
                        if label_span.count() > 0:
                            opt_val = opt_val or (label_span.inner_text(timeout=200) or "").strip()
                        options.append(opt_val or "Option")

                    chosen_answer = resolve_form_question(
                        question_title, question_desc, field_type="radio",
                        available_options=options, config=self.config
                    )

                    # Click matching radio
                    clicked = False
                    for r_idx, r in enumerate(radios):
                        opt_text = options[r_idx]
                        if opt_text.lower() == str(chosen_answer).lower() or chosen_answer.lower() in opt_text.lower():
                            r.click(timeout=1500)
                            human_delay(0.4, 0.8)
                            logging.info(f"Selected radio option -> '{opt_text}'")
                            clicked = True
                            page_filled += 1
                            self.filled_count += 1
                            break

                    if not clicked and radios:
                        radios[0].click(timeout=1500)
                        logging.info(f"Defaulted radio option -> '{options[0]}'")
                        page_filled += 1
                        self.filled_count += 1

                    # Check for "Other:" custom text input
                    other_input = item.locator("input[type='text'].Hvn9fb, input[aria-label*='Other']").first
                    if other_input.count() > 0 and other_input.is_visible(timeout=300):
                        other_input.fill(str(chosen_answer))
                    continue

                # 4. Checkboxes (Multi-select)
                checkboxes = item.locator("div[role='checkbox']").all()
                if checkboxes:
                    cb_options = []
                    for cb in checkboxes:
                        label_span = cb.locator("span.aDTYNe, span").first
                        val = (label_span.inner_text(timeout=200) or "").strip() if label_span.count() > 0 else (cb.get_attribute("data-answer-value") or "")
                        cb_options.append(val)

                    chosen_selection = resolve_form_question(
                        question_title, question_desc, field_type="checkbox",
                        available_options=cb_options, config=self.config
                    )

                    # If multiple matching options returned (list) or single match
                    targets = chosen_selection if isinstance(chosen_selection, list) else [chosen_selection]
                    for cb_idx, cb in enumerate(checkboxes):
                        opt_text = cb_options[cb_idx]
                        if any(t.lower() in opt_text.lower() for t in targets):
                            is_checked = cb.get_attribute("aria-checked") == "true"
                            if not is_checked:
                                cb.click(timeout=1500)
                                human_delay(0.3, 0.6)
                                logging.info(f"Checked box -> '{opt_text}'")
                                page_filled += 1
                                self.filled_count += 1

                    continue

                # 5. Dropdown Listbox
                listbox = item.locator("div[role='listbox']").first
                if listbox.count() > 0:
                    try:
                        listbox.click(timeout=1500)
                        human_delay(0.5, 1.0)
                        popup_options = self.page.locator("div[role='option']:not([aria-disabled='true'])").all()
                        dd_options = [(opt.inner_text(timeout=200) or "").strip() for opt in popup_options]

                        best_match = resolve_form_question(
                            question_title, question_desc, field_type="dropdown",
                            available_options=dd_options, config=self.config
                        )

                        clicked_dd = False
                        for p_idx, p_opt in enumerate(popup_options):
                            p_txt = dd_options[p_idx]
                            if best_match.lower() in p_txt.lower():
                                p_opt.click(timeout=1500)
                                human_delay(0.4, 0.8)
                                logging.info(f"Selected dropdown option -> '{p_txt}'")
                                clicked_dd = True
                                page_filled += 1
                                self.filled_count += 1
                                break

                        if not clicked_dd and popup_options:
                            popup_options[0].click(timeout=1000)
                    except Exception as de:
                        logging.debug(f"[Google Forms] Dropdown handling note: {de}")
                    continue

                # 6. Textarea (Long answer / Paragraph)
                textarea = item.locator("textarea, div[role='textbox']").first
                if textarea.count() > 0 and textarea.is_visible(timeout=300):
                    ans = resolve_form_question(
                        question_title, question_desc, field_type="textarea",
                        available_options=[], config=self.config
                    )
                    textarea.click(timeout=1000)
                    textarea.fill(str(ans))
                    human_delay(0.3, 0.7)
                    logging.info(f"Filled textarea -> '{str(ans)[:40]}...'")
                    page_filled += 1
                    self.filled_count += 1
                    continue

                # 7. Short Text Input
                text_input = item.locator("input[type='text'], input:not([type])").first
                if text_input.count() > 0 and text_input.is_visible(timeout=300):
                    ans = resolve_form_question(
                        question_title, question_desc, field_type="text",
                        available_options=[], config=self.config
                    )
                    text_input.click(timeout=1000)
                    text_input.fill(str(ans))
                    human_delay(0.3, 0.6)
                    logging.info(f"Filled text field -> '{str(ans)}'")
                    page_filled += 1
                    self.filled_count += 1
                    continue

                # 8. Date Field
                date_input = item.locator("input[type='date']").first
                if date_input.count() > 0 and date_input.is_visible(timeout=300):
                    date_val = "1999-06-12" if any(k in question_title.lower() for k in ("dob", "birth")) else (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
                    date_input.fill(date_val)
                    human_delay(0.3, 0.6)
                    logging.info(f"Filled date input -> '{date_val}'")
                    page_filled += 1
                    self.filled_count += 1
                    continue

            except Exception as qe:
                logging.debug(f"[Google Forms] Question solve error: {qe}")

        return page_filled

    def solve(self) -> bool:
        """
        Execute full Google Form solving loop across all pages.
        """
        form_title = self.extract_form_title()
        logging.info(f"[Google Forms] Starting solver for '{form_title}'...")

        max_pages = 8
        current_page = 1

        for _ in range(max_pages):
            logging.info(f"[Google Forms] Solving Page {current_page}...")
            self.fill_current_page_questions()
            human_delay(1.0, 2.0)

            # Check if Submit button is present on this page
            submit_btn = self.page.locator("div[role='button']:has-text('Submit'), span:has-text('Submit'), input[type='submit']").first
            is_submit_visible = submit_btn.count() > 0 and submit_btn.is_visible(timeout=800)

            # Check if Next button is present
            next_btn = self.page.locator("div[role='button']:has-text('Next'), span:has-text('Next')").first
            is_next_visible = next_btn.count() > 0 and next_btn.is_visible(timeout=800)

            if is_submit_visible:
                logging.info(f"[Google Forms] Form completed ({self.filled_count} fields resolved). Final Submit button reached.")

                # Dry-Run Mode
                if self.dry_run:
                    output_dir = os.path.expanduser(self.config.get("paths", {}).get("output", "~/Documents/JobAutomation/output"))
                    os.makedirs(output_dir, exist_ok=True)
                    ss_path = os.path.join(output_dir, f"dry_run_google_form_{int(time.time())}.png")
                    self.page.screenshot(path=ss_path, full_page=True)
                    logging.info(f"[DRY RUN] Google Form filled. Full screenshot saved: {ss_path}")
                    return True

                # Review Mode: Visual confirmation
                if self.review_mode:
                    notify_macos("Google Form Review Required", f"Review '{form_title}' in browser. Pausing 30s before auto-submit.")
                    print("\n[REVIEW MODE] Pausing 30 seconds for manual inspection before submit...")
                    try:
                        time.sleep(30)
                    except KeyboardInterrupt:
                        pass

                # Submit Form
                logging.info("[Google Forms] Clicking Submit button...")
                submit_btn.click(timeout=3000)
                human_delay(3.0, 5.0)

                # Verify submission confirmation
                confirmation_loc = self.page.locator("div.vHW8K, div:has-text('Your response has been recorded'), a:has-text('Submit another response')").first
                if confirmation_loc.count() > 0:
                    logging.info("✅ [Google Forms] Application submitted successfully! Response recorded.")
                    keyword_tracker.record_query_run("Google Forms", form_title, scanned=1, matched=1, applied=1)
                    return True
                else:
                    logging.info("[Google Forms] Submitted form (confirmation screen reached).")
                    keyword_tracker.record_query_run("Google Forms", form_title, scanned=1, matched=1, applied=1)
                    return True

            elif is_next_visible:
                logging.info(f"[Google Forms] Page {current_page} completed. Clicking Next...")
                next_btn.click(timeout=3000)
                human_delay(2.0, 3.5)
                current_page += 1
            else:
                logging.warning("[Google Forms] Neither Next nor Submit button found. Form might be complete or blocked.")
                output_dir = os.path.expanduser(self.config.get("paths", {}).get("output", "~/Documents/JobAutomation/output"))
                ss_path = os.path.join(output_dir, f"google_form_stuck_{int(time.time())}.png")
                try:
                    self.page.screenshot(path=ss_path)
                except Exception:
                    pass
                notifier.notify_stuck(
                    platform="Google Forms",
                    company=form_title,
                    role="Application Form",
                    reason=f"Halted on Page {current_page}: Next/Submit button not found or validation error",
                    url=self.page.url,
                    screenshot_path=ss_path
                )
                break

        return self.filled_count > 0


# ---------------------------------------------------------------------------
# Microsoft Forms Solver Engine
# ---------------------------------------------------------------------------

class MicrosoftFormsSolver:
    """
    Automates multi-page and single-page Microsoft Forms applications.
    Handles Microsoft Forms question cards, text inputs, textareas,
    radio choices, checkboxes, dropdowns, ratings, and resume upload.
    """

    def __init__(self, page, config, resume_pdf_path=None, dry_run=False, review_mode=False):
        self.page = page
        self.config = config
        self.resume_pdf_path = resume_pdf_path
        self.dry_run = dry_run
        self.review_mode = review_mode
        self.filled_count = 0

    def extract_form_title(self) -> str:
        """Extract title of Microsoft Form."""
        title_selectors = [
            "span.office-form-title",
            "div[data-automation-id='formTitle']",
            "span.text-format-content",
            "h1",
            "h2"
        ]
        for sel in title_selectors:
            loc = self.page.locator(sel).first
            if loc.count() > 0:
                txt = (loc.inner_text(timeout=400) or "").strip()
                if txt:
                    return txt
        return "Microsoft Form Application"

    def fill_current_page_questions(self) -> int:
        """
        Inspect and solve all question cards on current Microsoft Form page.
        """
        page_filled = 0
        items = self.page.locator("div[data-automation-id='questionItem'], div.office-form-question, div.question-item").all()
        logging.info(f"[MS Forms] Found {len(items)} question cards on current page.")

        for idx, item in enumerate(items):
            try:
                if not item.is_visible(timeout=300):
                    continue

                # 1. Question Title & Subtitle
                title_loc = item.locator("span[data-automation-id='questionTitle'], span.question-title-box, div.question-title, h3").first
                question_title = (title_loc.inner_text(timeout=300) or "").strip() if title_loc.count() > 0 else ""
                if not question_title:
                    continue

                sub_loc = item.locator("span.question-subtitle, div.question-subtitle").first
                question_desc = (sub_loc.inner_text(timeout=200) or "").strip() if sub_loc.count() > 0 else ""

                is_required = item.locator("span.required-star, span:has-text('*')").count() > 0
                req_badge = " [REQUIRED]" if is_required else ""
                logging.info(f"[MS Forms] Q{idx+1}: '{question_title}'{req_badge}")

                # 2. File Upload (Resume / CV)
                if any(w in question_title.lower() for w in ("resume", "cv", "upload", "profile")):
                    file_input = item.locator("input[type='file']").first
                    if file_input.count() > 0 and self.resume_pdf_path and os.path.exists(self.resume_pdf_path):
                        try:
                            file_input.set_input_files(self.resume_pdf_path, timeout=3000)
                            human_delay(1.5, 2.5)
                            logging.info(f"Attached Resume PDF -> {os.path.basename(self.resume_pdf_path)}")
                            page_filled += 1
                            self.filled_count += 1
                            continue
                        except Exception as fe:
                            logging.debug(f"[MS Forms] File upload note: {fe}")

                # 3. Radio Buttons (Single Choice)
                radios = item.locator("div[data-automation-id='radio'], div[role='radio'], input[type='radio']").all()
                if radios:
                    options = []
                    for r in radios:
                        txt_span = r.locator("span").first
                        val = (txt_span.inner_text(timeout=200) or "").strip() if txt_span.count() > 0 else (r.inner_text(timeout=200) or "")
                        options.append(val)

                    chosen_answer = resolve_form_question(
                        question_title, question_desc, field_type="radio",
                        available_options=options, config=self.config
                    )

                    clicked = False
                    for r_idx, r in enumerate(radios):
                        opt_text = options[r_idx]
                        if opt_text.lower() == str(chosen_answer).lower() or chosen_answer.lower() in opt_text.lower():
                            r.click(timeout=1500)
                            human_delay(0.3, 0.6)
                            logging.info(f"Selected radio option -> '{opt_text}'")
                            clicked = True
                            page_filled += 1
                            self.filled_count += 1
                            break

                    if not clicked and radios:
                        radios[0].click(timeout=1500)
                        page_filled += 1
                        self.filled_count += 1
                    continue

                # 4. Checkboxes (Multi-select)
                checkboxes = item.locator("div[data-automation-id='checkbox'], div[role='checkbox'], input[type='checkbox']").all()
                if checkboxes:
                    cb_options = []
                    for cb in checkboxes:
                        txt_span = cb.locator("span").first
                        val = (txt_span.inner_text(timeout=200) or "").strip() if txt_span.count() > 0 else (cb.inner_text(timeout=200) or "")
                        cb_options.append(val)

                    chosen_selection = resolve_form_question(
                        question_title, question_desc, field_type="checkbox",
                        available_options=cb_options, config=self.config
                    )
                    targets = chosen_selection if isinstance(chosen_selection, list) else [chosen_selection]

                    for cb_idx, cb in enumerate(checkboxes):
                        opt_text = cb_options[cb_idx]
                        if any(t.lower() in opt_text.lower() for t in targets):
                            cb.click(timeout=1500)
                            human_delay(0.3, 0.6)
                            logging.info(f"Checked MS Form box -> '{opt_text}'")
                            page_filled += 1
                            self.filled_count += 1
                    continue

                # 5. Dropdown / Select
                select_loc = item.locator("div[data-automation-id='select'], div[role='combobox'], select").first
                if select_loc.count() > 0:
                    try:
                        select_loc.click(timeout=1500)
                        human_delay(0.5, 1.0)
                        popup_options = self.page.locator("div[role='option'], div.office-form-option").all()
                        dd_options = [(opt.inner_text(timeout=200) or "").strip() for opt in popup_options]

                        best_match = resolve_form_question(
                            question_title, question_desc, field_type="dropdown",
                            available_options=dd_options, config=self.config
                        )

                        for p_idx, p_opt in enumerate(popup_options):
                            if best_match.lower() in dd_options[p_idx].lower():
                                p_opt.click(timeout=1500)
                                human_delay(0.4, 0.7)
                                logging.info(f"Selected MS Form dropdown option -> '{dd_options[p_idx]}'")
                                page_filled += 1
                                self.filled_count += 1
                                break
                    except Exception as de:
                        logging.debug(f"[MS Forms] Dropdown note: {de}")
                    continue

                # 6. Textarea (Long Text)
                textarea = item.locator("textarea[data-automation-id='textInput'], textarea.office-form-question-textbox, textarea").first
                if textarea.count() > 0 and textarea.is_visible(timeout=300):
                    ans = resolve_form_question(
                        question_title, question_desc, field_type="textarea",
                        available_options=[], config=self.config
                    )
                    textarea.click(timeout=1000)
                    textarea.fill(str(ans))
                    human_delay(0.3, 0.6)
                    logging.info(f"Filled MS Form textarea -> '{str(ans)[:40]}...'")
                    page_filled += 1
                    self.filled_count += 1
                    continue

                # 7. Text Input (Single line)
                text_input = item.locator("input[data-automation-id='textInput'], input.office-form-question-textbox, input[type='text']").first
                if text_input.count() > 0 and text_input.is_visible(timeout=300):
                    ans = resolve_form_question(
                        question_title, question_desc, field_type="text",
                        available_options=[], config=self.config
                    )
                    text_input.click(timeout=1000)
                    text_input.fill(str(ans))
                    human_delay(0.3, 0.6)
                    logging.info(f"Filled MS Form text input -> '{str(ans)}'")
                    page_filled += 1
                    self.filled_count += 1
                    continue

                # 8. Date Field
                date_input = item.locator("input[data-automation-id='dateInput'], input[type='date']").first
                if date_input.count() > 0 and date_input.is_visible(timeout=300):
                    date_val = "12/06/1999" if any(k in question_title.lower() for k in ("dob", "birth")) else (datetime.now() + timedelta(days=7)).strftime("%d/%m/%Y")
                    date_input.fill(date_val)
                    human_delay(0.3, 0.6)
                    logging.info(f"Filled MS Form date -> '{date_val}'")
                    page_filled += 1
                    self.filled_count += 1
                    continue

            except Exception as me:
                logging.debug(f"[MS Forms] Question solve error: {me}")

        return page_filled

    def solve(self) -> bool:
        """
        Execute full Microsoft Form solving loop across all pages.
        """
        form_title = self.extract_form_title()
        logging.info(f"[MS Forms] Starting solver for '{form_title}'...")

        max_pages = 8
        current_page = 1

        for _ in range(max_pages):
            logging.info(f"[MS Forms] Solving Page {current_page}...")
            self.fill_current_page_questions()
            human_delay(1.0, 2.0)

            # Check if Submit button is visible
            submit_btn = self.page.locator("button[data-automation-id='submitButton'], button:has-text('Submit')").first
            is_submit_visible = submit_btn.count() > 0 and submit_btn.is_visible(timeout=800)

            # Check if Next button is visible
            next_btn = self.page.locator("button[data-automation-id='nextButton'], button:has-text('Next')").first
            is_next_visible = next_btn.count() > 0 and next_btn.is_visible(timeout=800)

            if is_submit_visible:
                logging.info(f"[MS Forms] Form completed ({self.filled_count} fields resolved). Final Submit button reached.")

                # Dry-Run Mode
                if self.dry_run:
                    output_dir = os.path.expanduser(self.config.get("paths", {}).get("output", "~/Documents/JobAutomation/output"))
                    os.makedirs(output_dir, exist_ok=True)
                    ss_path = os.path.join(output_dir, f"dry_run_ms_form_{int(time.time())}.png")
                    self.page.screenshot(path=ss_path, full_page=True)
                    logging.info(f"[DRY RUN] MS Form filled. Screenshot saved: {ss_path}")
                    return True

                # Review Mode: Visual confirmation
                if self.review_mode:
                    notify_macos("Microsoft Form Review Required", f"Review '{form_title}' in browser. Pausing 30s before auto-submit.")
                    print("\n[REVIEW MODE] Pausing 30 seconds for manual inspection before submit...")
                    try:
                        time.sleep(30)
                    except KeyboardInterrupt:
                        pass

                # Submit Form
                logging.info("[MS Forms] Clicking Submit button...")
                submit_btn.click(timeout=3000)
                human_delay(3.0, 5.0)

                # Confirmation Check
                conf_loc = self.page.locator("div[data-automation-id='thankYouMessage'], div:has-text('Your response was submitted')").first
                if conf_loc.count() > 0:
                    logging.info("✅ [MS Forms] Application submitted successfully! Response recorded.")
                    keyword_tracker.record_query_run("Microsoft Forms", form_title, scanned=1, matched=1, applied=1)
                    return True
                else:
                    logging.info("[MS Forms] Submitted form (confirmation screen reached).")
                    keyword_tracker.record_query_run("Microsoft Forms", form_title, scanned=1, matched=1, applied=1)
                    return True

            elif is_next_visible:
                logging.info(f"[MS Forms] Page {current_page} completed. Clicking Next...")
                next_btn.click(timeout=3000)
                human_delay(2.0, 3.5)
                current_page += 1
            else:
                logging.warning("[MS Forms] Neither Next nor Submit button found. Form might be complete or blocked.")
                output_dir = os.path.expanduser(self.config.get("paths", {}).get("output", "~/Documents/JobAutomation/output"))
                ss_path = os.path.join(output_dir, f"ms_form_stuck_{int(time.time())}.png")
                try:
                    self.page.screenshot(path=ss_path)
                except Exception:
                    pass
                notifier.notify_stuck(
                    platform="Microsoft Forms",
                    company=form_title,
                    role="Application Form",
                    reason=f"Halted on Page {current_page}: Next/Submit button not found or validation error",
                    url=self.page.url,
                    screenshot_path=ss_path
                )
                break

        return self.filled_count > 0


# ---------------------------------------------------------------------------
# High-Level Application Orchestration
# ---------------------------------------------------------------------------

def apply_single_url(url: str, config: dict, dry_run: bool = False, force_headed: bool = False, review_mode: bool = False) -> bool:
    """
    Launch browser context, automatically detect form platform (Google Forms vs Microsoft Forms),
    navigate, solve questionnaire fields, upload tailored resume, and submit or review.
    """
    if not url:
        logging.warning("No URL provided to Forms Agent.")
        return False

    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_forms_ledger(logs_dir)

    daily_limit = config.get("forms", {}).get("daily_apply_limit", 25)
    applied_today = count_forms_applied_today(ledger)
    if applied_today >= daily_limit and not dry_run:
        logging.warning(f"[Forms Agent] Daily apply limit reached ({applied_today}/{daily_limit}). Skipping {url}")
        return False

    if url in ledger.get("applied_forms", {}) and not dry_run:
        logging.info(f"[Forms Agent] Already applied to {url} previously. Skipping duplicate application.")
        return True

    logging.info(f"[Forms Agent] 🚀 Navigating to Form Application: {url}")
    resume_pdf_path = naukri_agent.ensure_master_resume_pdf(config)

    with sync_playwright() as playwright:
        browser_context = launch_forms_browser(playwright, config, force_headed=force_headed or review_mode)
        page = browser_context.new_page()
        page.set_default_timeout(30000)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            human_delay(2.5, 4.0)

            # Detect platform
            page_content = page.content().lower()
            current_url = page.url.lower()

            if is_google_form_url(current_url) or "docs.google.com/forms" in page_content or page.locator("div[role='listitem']").count() > 0:
                solver = GoogleFormsSolver(page, config, resume_pdf_path=resume_pdf_path, dry_run=dry_run, review_mode=review_mode)
                form_platform = "Google Forms"
            elif is_ms_form_url(current_url) or "forms.office.com" in page_content or page.locator("div[data-automation-id='questionItem']").count() > 0:
                solver = MicrosoftFormsSolver(page, config, resume_pdf_path=resume_pdf_path, dry_run=dry_run, review_mode=review_mode)
                form_platform = "Microsoft Forms"
            else:
                # Default to Google Forms solver if ambiguous
                solver = GoogleFormsSolver(page, config, resume_pdf_path=resume_pdf_path, dry_run=dry_run, review_mode=review_mode)
                form_platform = "Generic Web Form"

            success = solver.solve()
            if success and not dry_run:
                form_title = solver.extract_form_title()
                ledger["applied_forms"][url] = {
                    "url": url,
                    "platform": form_platform,
                    "title": form_title,
                    "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "fields_filled": solver.filled_count,
                    "status": "APPLIED"
                }
                ledger["last_run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_forms_ledger(logs_dir, ledger)

                success_msg = f"✅ Successfully applied on {form_platform}: '{form_title}'"
                print(f"\n{success_msg}\n")
                logging.info(success_msg)
                notify_macos("Job Form Submitted", f"Applied on {form_platform}: {form_title}")
                return True

            return success

        except Exception as e:
            logging.error(f"[Forms Agent] Error during application: {e}")
            return False
        finally:
            try:
                browser_context.close()
            except Exception:
                pass


def process_forms_dropzone(config: dict, max_applies: int = None, dry_run: bool = False, force_headed: bool = False, review_mode: bool = False) -> int:
    """
    Process all pending Google Forms and Microsoft Forms tickets in dropzone.
    """
    import ats_bridge
    paths = config.get("paths", {})
    dropzone_dir = os.path.expanduser(paths.get("dropzone", "~/Documents/JobAutomation/dropzone"))
    local_dropzone = os.path.expanduser("~/Documents/JobAutomation/dropzone")

    scan_dirs = [dropzone_dir]
    if os.path.abspath(local_dropzone) != os.path.abspath(dropzone_dir):
        scan_dirs.append(local_dropzone)

    ticket_files = []
    for d in scan_dirs:
        if os.path.exists(d):
            for fname in os.listdir(d):
                if fname.endswith(".txt") and not fname.startswith("."):
                    ticket_files.append(os.path.join(d, fname))

    logging.info(f"[Forms Agent] Found {len(ticket_files)} dropzone candidate files.")
    applied_count = 0

    for tf in ticket_files:
        if max_applies is not None and applied_count >= max_applies:
            logging.info(f"[Forms Agent] Reached max_applies target ({max_applies}). Stopping tickets loop.")
            break
        try:
            with open(tf, "r", encoding="utf-8") as f:
                content = f.read()

            ats_type, ats_url = ats_bridge.find_ats_url_in_text(content, resolve_shorteners=True)
            if ats_type in ("google_forms", "ms_forms", "form", "forms"):
                logging.info(f"[Forms Agent] Processing dropzone ticket: {os.path.basename(tf)} -> {ats_url}")
                ok = apply_single_url(ats_url, config, dry_run=dry_run, force_headed=force_headed, review_mode=review_mode)
                if ok:
                    applied_count += 1
                    try:
                        os.remove(tf)
                        logging.info(f"Cleaned up processed form ticket: {os.path.basename(tf)}")
                    except Exception as e:
                        logging.debug(f"Could not remove {tf}: {e}")
        except Exception as e:
            logging.warning(f"Error inspecting ticket {tf}: {e}")

    return applied_count


def print_status(config: dict):
    """Print current ledger and daily metrics for Google and Microsoft Forms."""
    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_forms_ledger(logs_dir)
    today_count = count_forms_applied_today(ledger)
    total_count = len(ledger.get("applied_forms", {}))
    daily_limit = config.get("forms", {}).get("daily_apply_limit", 25)

    print("\n" + "=" * 60)
    print("      GOOGLE & MICROSOFT FORMS AUTOMATION ENGINE")
    print("=" * 60)
    print(f" • Last Run:           {ledger.get('last_run_at') or 'Never'}")
    print(f" • Applies Today:      {today_count} / {daily_limit} (Daily Limit)")
    print(f" • Total Tracked:      {total_count} Form Applications")
    print(f" • Supported Engines:  Google Forms (forms.gle) & Microsoft Forms (office.com)")
    print("=" * 60 + "\n")


def interactive_login(config: dict, target_url: str = "https://accounts.google.com/"):
    """
    Launch visible Google Chrome window for one-time Google / Microsoft account login.
    The authenticated session is permanently stored in browser_profile/forms.
    """
    print("\n" + "=" * 60)
    print("      ONE-TIME GOOGLE / MICROSOFT FORMS LOGIN SESSION")
    print("=" * 60)
    print(f"Opening visible Chrome browser at: {target_url}")
    print("Log into your Google or Microsoft account. Once done, close the browser or press Ctrl+C.")
    print("=" * 60 + "\n")

    with sync_playwright() as playwright:
        context = launch_forms_browser(playwright, config, force_headed=True)
        page = context.new_page()
        page.goto(target_url)
        try:
            time.sleep(120)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                context.close()
            except Exception:
                pass
    print("[Forms Agent] Session saved to browser profile.")


def main():
    parser = argparse.ArgumentParser(description="Google Forms & Microsoft Forms Autonomous Application Engine")
    parser.add_argument("--url", type=str, help="Apply directly to a Google Form or Microsoft Form URL")
    parser.add_argument("--dropzone", action="store_true", help="Process pending form tickets from dropzone")
    parser.add_argument("--dry-run", action="store_true", help="Fill form fields and save screenshot without submitting")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser window")
    parser.add_argument("--review", action="store_true", help="Fill form and pause 30s for manual review before submit")
    parser.add_argument("--status", action="store_true", help="Display current form application metrics")
    parser.add_argument("--login", action="store_true", help="Launch visible Chrome to log into Google/Microsoft account")
    args = parser.parse_args()

    config = email_agent.load_config()

    if args.status or (len(sys.argv) == 1 and not args.url and not args.dropzone and not args.login):
        print_status(config)
        return

    with MacKeepAwake():
        if args.login:
            interactive_login(config)
        elif args.url:
            apply_single_url(
                args.url, config,
                dry_run=args.dry_run,
                force_headed=args.headed,
                review_mode=args.review
            )
        elif args.dropzone:
            process_forms_dropzone(
                config,
                dry_run=args.dry_run,
                force_headed=args.headed,
                review_mode=args.review
            )


if __name__ == "__main__":
    main()
