#!/usr/bin/env python3
"""
Greenhouse Automated Job Discovery & Form Solver Engine
Autonomous ATS agent for Amrit Kumar.
Supports:
  1. Automated Greenhouse Board Sweeping (public boards-api.greenhouse.io)
  2. Direct Greenhouse Job URL Application & Modal Solving (native & embedded iframes)
  3. Tailored Resume & Contact Auto-Filling
  4. QA Bank & Ollama Fallback for Custom Screening Questions
  5. Dry-Run & Review Modes with Persistent Ledger & Mac Notifications
"""

import os
import sys
import re
import json
import time
import random
import logging
import argparse
import ssl
import urllib.request
from datetime import datetime

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
    format='%(asctime)s [%(levelname)s] [Greenhouse] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


def human_delay(min_sec=1.5, max_sec=3.5):
    """Sleep for a randomized human-like interval."""
    time.sleep(random.uniform(min_sec, max_sec))


def notify_macos(title, message, is_urgent=False):
    """Display a non-blocking macOS notification banner respecting quiet_mode."""
    power_utils.notify_macos(title, message, is_urgent=is_urgent)


def get_ssl_context():
    """Create an SSL context capable of handling Greenhouse API requests."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl._create_unverified_context()


# ---------------------------------------------------------------------------
# Ledger Management
# ---------------------------------------------------------------------------

def load_greenhouse_ledger(logs_dir):
    """Load persistent ledger of applied Greenhouse jobs."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'greenhouse_applied_jobs.json')
    default_data = {
        "last_run_at": None,
        "applied_jobs": {}
    }
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    default_data.update(data)
                    if not isinstance(default_data.get("applied_jobs"), dict):
                        default_data["applied_jobs"] = {}
        except Exception as e:
            logging.warning(f"Could not read greenhouse_applied_jobs.json: {e}")
    return default_data


def save_greenhouse_ledger(logs_dir, ledger):
    """Persist updated ledger to greenhouse_applied_jobs.json."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'greenhouse_applied_jobs.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.warning(f"Could not save greenhouse_applied_jobs.json: {e}")


def count_applies_today(ledger):
    """Count how many Greenhouse applications were submitted today (YYYY-MM-DD)."""
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    applied = ledger.get("applied_jobs", {})
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
# Greenhouse Board API Discovery
# ---------------------------------------------------------------------------

def fetch_board_jobs(board_token, timeout=8):
    """
    Fetch all active jobs from Greenhouse public board API.
    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    ctx = get_ssl_context()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data.get("jobs", [])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logging.debug(f"Board '{board_token}' returned 404 Not Found.")
        else:
            logging.warning(f"HTTP {e.code} fetching board '{board_token}': {e.reason}")
        return []
    except Exception as e:
        logging.warning(f"Failed to fetch jobs for board '{board_token}': {e}")
        return []


def is_job_matching(job, gh_cfg):
    """
    Evaluate if a Greenhouse job posting matches the candidate's target roles and locations.
    """
    title = (job.get("title") or "").strip().lower()
    if not title:
        return False

    # 1. Check excluded keywords
    excluded = [k.lower() for k in gh_cfg.get("excluded_title_keywords", [])]
    for ex in excluded:
        if ex and ex in title:
            return False

    # 2. Check title queries / target tech keywords
    queries = [q.lower() for q in gh_cfg.get("queries", [])]
    target_stems = [
        "full stack", "fullstack", "software engineer", "software developer",
        "backend", "back-end", "back end", "java", "python", "spring boot",
        "spring", "react", "fastapi", "sde", "mts", "member of technical staff"
    ]
    all_targets = set(queries + target_stems)
    title_matches = any(t in title for t in all_targets)
    if not title_matches:
        return False

    # 3. Location filtering
    loc_name = ""
    loc_obj = job.get("location")
    if isinstance(loc_obj, dict):
        loc_name = (loc_obj.get("name") or "").lower()
    elif isinstance(loc_obj, str):
        loc_name = loc_obj.lower()

    # Also inspect offices metadata if available
    offices = job.get("offices", [])
    office_names = " ".join((o.get("name") or "").lower() for o in offices if isinstance(o, dict))
    combined_loc = f"{loc_name} {office_names}".strip()

    target_locs = [l.lower() for l in gh_cfg.get("locations", [])]
    # If no target locations specified or contains "anywhere", accept all
    if not target_locs or "anywhere" in target_locs:
        return True

    # Check if target location (India, Bengaluru, Remote, etc.) is in the job location
    loc_matches = any(tl in combined_loc for tl in target_locs)
    # If job says remote / flexible / worldwide, accept it
    if "remote" in combined_loc or "worldwide" in combined_loc or "flexible" in combined_loc or not combined_loc:
        loc_matches = True

    return loc_matches


def search_greenhouse_jobs(config, ledger, max_jobs=50):
    """
    Discover candidate jobs across all configured Greenhouse target boards.
    Returns a list of job dicts formatted for application.
    """
    gh_cfg = config.get("greenhouse", {})
    boards = gh_cfg.get("target_boards", [])
    if not boards:
        logging.info("No target_boards configured in config.json greenhouse section.")
        return []

    applied_keys = set(ledger.get("applied_jobs", {}).keys())
    matched_jobs = []

    # Known boards that redirect away from standard Greenhouse ATS forms to proprietary frontends
    custom_frontend_boards = {"stripe"}

    per_board_cap = max(3, max_jobs // max(1, len(boards)))

    logging.info(f"Scanning {len(boards)} Greenhouse company boards for engineering roles (cap: {per_board_cap}/board)...")
    for board in boards:
        if board.lower() in custom_frontend_boards:
            continue

        jobs = fetch_board_jobs(board)
        if not jobs:
            continue

        board_matched = 0
        board_added = 0
        for j in jobs:
            job_id = str(j.get("id"))
            canonical_url = j.get("absolute_url") or f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}"
            
            # Skip already applied
            if job_id in applied_keys or canonical_url in applied_keys:
                continue

            if is_job_matching(j, gh_cfg):
                board_matched += 1
                loc_str = j.get("location", {}).get("name", "Unknown Location") if isinstance(j.get("location"), dict) else str(j.get("location"))
                matched_jobs.append({
                    "board": board,
                    "id": job_id,
                    "title": j.get("title"),
                    "company": board.capitalize(),
                    "location": loc_str,
                    "url": canonical_url,
                    "updated_at": j.get("updated_at")
                })
                board_added += 1
                if board_added >= per_board_cap or len(matched_jobs) >= max_jobs:
                    break

        keyword_tracker.record_query_run(f"Greenhouse ({board})", "Engineering Roles", scanned=len(jobs), matched=board_matched, applied=0)
        time.sleep(0.2)  # Polite pacing across boards

    logging.info(f"Discovered {len(matched_jobs)} new matching jobs across Greenhouse boards.")
    return matched_jobs


# ---------------------------------------------------------------------------
# Browser Initialization
# ---------------------------------------------------------------------------

def launch_greenhouse_browser(playwright, config, force_headed=False):
    """Launch persistent Google Chrome context dedicated to Greenhouse automation."""
    gh_cfg = config.get('greenhouse', {})
    profile_dir = os.path.expanduser(
        gh_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/greenhouse')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = gh_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(gh_cfg.get('headless', True))

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


def get_greenhouse_target(page):
    """
    Return either the page itself or the Greenhouse iframe frame_locator
    if the application is embedded in a company career site.
    """
    try:
        iframe_selectors = [
            "iframe[src*='greenhouse.io']",
            "iframe#grnhse_iframe",
            "iframe[name*='greenhouse']",
            "iframe[title*='Greenhouse']",
            "iframe[title*='Job Application']"
        ]
        for sel in iframe_selectors:
            frame = page.locator(sel).first
            if frame.count() > 0 and frame.is_visible(timeout=1500):
                logging.debug(f"Detected embedded Greenhouse iframe: {sel}")
                return page.frame_locator(sel).first
    except Exception:
        pass
    return page


# ---------------------------------------------------------------------------
# Form Solver & Field Resolver
# ---------------------------------------------------------------------------

def answer_custom_question(label_text, config, available_options=None):
    """
    Intelligently determine the best answer for a custom Greenhouse question
    using Universal AI Question Solver (Memory, Heuristics, Ollama LLM).
    """
    if not label_text:
        return available_options[0] if available_options else ""

    text = (label_text or "").strip().lower()

    # 0. Check universal persistent QA memory first
    saved = qa_memory.recall_answer(label_text, available_options=available_options)
    if saved:
        logging.info(f"[QA Memory] Auto-recalled learned answer for '{label_text[:45]}...' -> '{saved}'")
        return saved

    # 0b. Check AI deterministic semantic classifier
    solv = ai_question_solver.classify_and_solve_deterministic(label_text, options=available_options, config=config)
    if solv:
        logging.info(f"[AI Solver] Deterministically resolved '{label_text[:45]}...' -> '{solv}'")
        try:
            qa_memory.save_learned_answer(label_text, solv, platform="Greenhouse")
        except Exception:
            pass
        return solv

    qa = config.get("naukri", {}).get("qa_bank", {})
    profile = config.get("profile", {})

    # 0. Candidate Location & City
    if any(k in text for k in ("location (city)", "candidate-location", "city of residence", "where are you located", "current city")):
        if not any(k in text for k in ("required to live", "office expectation", "live within")):
            if available_options:
                for opt in available_options:
                    if any(c in opt.lower() for c in ("bengaluru", "bangalore", "india", "remote")):
                        return opt
            return profile.get("current_location", "Bengaluru, India")

    # 1. LinkedIn & Social URLs
    if "linkedin" in text:
        return profile.get("linkedin", "https://linkedin.com/in/yourprofile")
    if any(k in text for k in ("github", "git hub", "repository", "portfolio", "personal website", "website")):
        return profile.get("github", "https://github.com/yourusername")

    # 2. Current / Past Company
    if any(k in text for k in ("current company", "most recent company", "recent company", "name of your current", "current employer", "present organization")):
        return profile.get("current_company", "Tech Solutions Inc.")

    # 3. Location elaboration / Open to relocation
    if any(k in text for k in ("tell us a bit more about your location", "location details", "if not, tell us", "willing to relocate")):
        return "Currently based in Bengaluru, India. Fully open to remote work or discussing relocation."

    # 4. In-Office / Residence requirement / Commute
    if any(k in text for k in ("required to live", "live within", "reside in", "commute", "in-office", "in office", "hybrid", "relocate", "relocation", "office location", "office expectation")):
        if available_options:
            for opt in available_options:
                if opt.lower() in ("yes", "i agree", "agree"):
                    return opt
        return "Yes"

    # 5. Notice Period / Availability
    if any(k in text for k in ("notice period", "notice", "how soon", "joining time", "start date", "availability")):
        days = qa.get("notice_period_days", "7")
        if available_options:
            for opt in available_options:
                opt_l = opt.lower()
                if "immediate" in opt_l or "1 week" in opt_l or "7 days" in opt_l or "less than 15" in opt_l:
                    return opt
        return f"{days} days (Immediate Joiner)"

    # 6. Salary / CTC / Compensation
    if any(k in text for k in ("ctc", "salary", "compensation", "package", "expected pay")):
        is_current = "current" in text or "present" in text
        val = qa.get("current_ctc_lpa", "12") if is_current else qa.get("expected_ctc_lpa", "15")
        if "lpa" in text or "inr" in text or "lakh" in text:
            return f"{val} LPA"
        return str(val)

    # 7. Experience / YOE
    if any(k in text for k in ("years of experience", "how many years", "total experience")):
        tot_exp = profile.get("total_experience", "3.5 Years")
        if available_options:
            for opt in available_options:
                if any(k in opt.lower() for k in ("3", "3-5", "3 to 5", "3+")):
                    return opt
        return "3.5"

    # 8. Work Authorization & Sponsorship
    if any(k in text for k in ("authorized to work in the u.s", "authorized to work in the us", "us work authorization")):
        return "No"
    if any(k in text for k in ("authorized to work in india", "authorized to work in your country", "legally authorized to work")):
        return "Yes"
    if any(k in text for k in ("unrestricted right to work", "country of work authorization")):
        return "India"
    if any(k in text for k in ("require sponsorship", "sponsorship to continue", "visa sponsorship")):
        if "india" in text:
            return "No"
        return "Yes" if "u.s." in text or "us" in text else "No"

    # 9. Sanctions & Restricted Jurisdictions (Cuba, Iran, Syria, etc.)
    if any(k in text for k in ("cuba", "iran", "syria", "north korea", "belarus", "crimea", "sanctioned")):
        return "No"

    # 10. Prior Employment / Worked here in past
    if any(k in text for k in ("previously employed", "worked here", "worked for", "employed by", "employed with", "in the past", "former employee")):
        if available_options:
            for opt in available_options:
                if opt.lower() == "no":
                    return opt
        return "No"

    # 11. How did you hear about us?
    if any(k in text for k in ("hear about", "heard about", "how did you find", "where did you hear", "source")):
        if available_options:
            for opt in available_options:
                if any(s in opt.lower() for s in ("linkedin", "company website", "job board", "online", "github")):
                    return opt
        return "LinkedIn"

    # 11b. Excitement / Database / Engineering interest
    if any(k in text for k in ("database internals", "what excites you", "why cockroach", "why do you want")):
        if available_options:
            for opt in available_options:
                if any(s in opt.lower() for s in ("the product", "the role", "culture", "company vision")):
                    return opt
        return "The product"

    # 12. Consent / Acknowledgements / Privacy
    if any(k in text for k in ("acknowledge", "agree", "consent", "confirm", "privacy statement", "terms")):
        if available_options:
            for opt in available_options:
                if any(s in opt.lower() for s in ("i agree", "agree", "yes", "i acknowledge", "acknowledge")):
                    return opt
        return "Yes"

    # 13. Pronouns
    if "pronoun" in text:
        if available_options:
            for opt in available_options:
                if "he/him" in opt.lower():
                    return opt
        return "He/Him/His"

    # 14. Accommodations
    if any(k in text for k in ("accommodation", "accessibility", "comfortable during your interview")):
        return "None"

    # 14. Complex / Behavioral Open-Ended Fallback: Query Local AI (Ollama)
    llm_ans = ai_question_solver.query_ai_for_answer(label_text, options=available_options, config=config)
    if llm_ans:
        try:
            qa_memory.save_learned_answer(label_text, llm_ans, platform="Greenhouse")
        except Exception:
            pass
        return llm_ans

    # 15. Generate Smart Contextual Fallback (Context-tailored, never a generic repeating string)
    fallback_ans = ai_question_solver.generate_contextual_fallback(label_text, options=available_options)

    # 16. Human-in-the-Loop Intervention via Telegram
    tg_cfg = config.get("notifications", {}).get("telegram", {})
    if tg_cfg.get("enabled") and tg_cfg.get("alert_on_unknown_question"):
        user_choice = notifier.notify_stuck(
            platform="Greenhouse ATS",
            company="Company",
            role="Application",
            reason="Unrecognized Screening Question",
            question=label_text,
            options=available_options,
            default_answer=fallback_ans
        )
        if user_choice:
            qa[label_text] = user_choice
            try:
                qa_memory.save_learned_answer(label_text, user_choice, platform="Greenhouse")
            except Exception:
                pass
            return user_choice

    return fallback_ans


def fill_react_select(target, select_input, answer_text, page=None):
    """
    Handle modern Greenhouse `react-select` combobox components.
    Clicks input, types answer, and selects option.
    """
    try:
        select_input.click(timeout=2000)
        time.sleep(0.3)
        # Type the keyword
        clean_text = answer_text.split()[0] if len(answer_text.split()) > 0 else answer_text
        select_input.fill(clean_text, timeout=2000)
        time.sleep(0.5)
        if page:
            page.keyboard.press("Enter")
        else:
            select_input.press("Enter")
        time.sleep(0.3)
        return True
    except Exception as e:
        logging.debug(f"Could not fill react-select with '{answer_text}': {e}")
        return False


def solve_greenhouse_form(page, target, config, resume_pdf_path, dry_run=False):
    """
    Inspect the Greenhouse application form, fill contact details, upload resume,
    and resolve all custom questions and screening fields.
    """
    profile = config.get("profile", {})
    qa = config.get("naukri", {}).get("qa_bank", {})

    full_name = profile.get("full_name", "Amrit Kumar").strip()
    name_parts = full_name.split(maxsplit=1)
    first_name = name_parts[0] if name_parts else "Amrit"
    last_name = name_parts[1] if len(name_parts) > 1 else "Kumar"
    email_addr = profile.get("email", "candidate@example.com")
    phone_num = profile.get("phone", "+91-9876543210")
    loc_val = profile.get("current_location", "Bengaluru, India")

    contact_mapping = [
        ("#first_name, input[name*='first_name' i]", first_name),
        ("#last_name, input[name*='last_name' i]", last_name),
        ("#preferred_name, input[name*='preferred_name' i]", first_name),
        ("#email, input[name*='email' i]", email_addr),
        ("#phone, input[type='tel'], input[name*='phone' i]", phone_num),
    ]

    for sel, val in contact_mapping:
        try:
            inp = target.locator(sel).first
            if inp.count() > 0 and inp.is_visible(timeout=800):
                current_val = (inp.input_value() or "").strip()
                if not current_val:
                    inp.fill(val, timeout=2000)
                    time.sleep(0.2)
                    logging.info(f"Filled contact field '{sel}' -> {val}")
        except Exception:
            pass

    # Location input (can be text or react-select)
    try:
        loc_inp = target.locator("#candidate-location, input[id*='location' i], input[name*='location' i]").first
        if loc_inp.count() > 0 and loc_inp.is_visible(timeout=800):
            current_val = (loc_inp.input_value() or "").strip()
            if not current_val:
                if loc_inp.get_attribute("role") == "combobox":
                    fill_react_select(target, loc_inp, "Bengaluru", page=page)
                else:
                    loc_inp.fill(loc_val, timeout=2000)
                logging.info(f"Filled location field -> {loc_val}")
    except Exception:
        pass

    # 2. Resume Upload
    if resume_pdf_path and os.path.exists(resume_pdf_path):
        resume_selectors = [
            "input[type='file'][id*='resume' i]",
            "input[type='file'][name*='resume' i]",
            "input[type='file'][data-qa='resume-upload']",
            "input[type='file']"
        ]
        uploaded = False
        for r_sel in resume_selectors:
            try:
                r_inp = target.locator(r_sel).first
                if r_inp.count() > 0:
                    r_inp.set_input_files(resume_pdf_path, timeout=4000)
                    human_delay(1.5, 2.5)
                    logging.info(f"Attached resume PDF -> {os.path.basename(resume_pdf_path)}")
                    uploaded = True
                    break
            except Exception as e:
                logging.debug(f"Resume upload selector {r_sel} failed: {e}")
        if not uploaded:
            logging.warning("Could not locate file input for resume attachment.")

    # 3. Cover Letter (Optional)
    if config.get("greenhouse", {}).get("attach_cover_letter", False):
        cl_path = os.path.join(
            os.path.expanduser(config.get("paths", {}).get("output", "~/Documents/JobAutomation/output")),
            "AMRIT_KUMAR_COVER_LETTER.pdf"
        )
        if os.path.exists(cl_path):
            try:
                cl_inp = target.locator("input[type='file'][id*='cover_letter' i], input[type='file'][name*='cover_letter' i]").first
                if cl_inp.count() > 0:
                    cl_inp.set_input_files(cl_path, timeout=3000)
                    logging.info("Attached optional cover letter.")
            except Exception:
                pass

    # 4. Custom & Screening Questions
    field_containers = target.locator("div.field, div[class*='field'], div[data-field]").all()
    logging.info(f"Analyzing {len(field_containers)} field containers for questions...")

    for f_container in field_containers:
        try:
            # Skip standard contact & resume fields
            inner_html = f_container.inner_html() or ""
            if any(k in inner_html for k in ('id="first_name"', 'id="last_name"', 'id="email"', 'id="phone"', 'id="resume"', 'id="candidate-location"', 'id="cover_letter"')):
                continue

            label_el = f_container.locator("label, legend, div[id*='label']").first
            label_text = (label_el.inner_text(timeout=500) or "").strip() if label_el.count() > 0 else ""
            if not label_text:
                continue

            # A. Check for react-select combobox
            combobox = f_container.locator("input[role='combobox'], .select__input").first
            if combobox.count() > 0 and combobox.is_visible(timeout=400):
                ans = answer_custom_question(label_text, config)
                fill_react_select(target, combobox, ans, page=page)
                logging.info(f"Answered combobox question '{label_text[:40]}...' -> {ans}")
                continue

            # B. Check for standard <select> dropdown
            native_select = f_container.locator("select").first
            if native_select.count() > 0 and native_select.is_visible(timeout=400):
                options = [opt.inner_text().strip() for opt in native_select.locator("option").all()]
                ans = answer_custom_question(label_text, config, available_options=options)
                try:
                    native_select.select_option(label=ans, timeout=1500)
                except Exception:
                    # Try by value or first match
                    for opt in options:
                        if ans.lower() in opt.lower():
                            native_select.select_option(label=opt, timeout=1500)
                            break
                logging.info(f"Answered native select question '{label_text[:40]}...' -> {ans}")
                continue

            # C. Check for radio buttons
            radios = f_container.locator("input[type='radio']").all()
            if radios:
                ans = answer_custom_question(label_text, config)
                for r in radios:
                    r_lbl = r.locator("..").inner_text().strip().lower()
                    if ans.lower() in r_lbl or (ans.lower() == "yes" and "yes" in r_lbl) or (ans.lower() == "no" and "no" in r_lbl):
                        try:
                            r.check(timeout=1000)
                            logging.info(f"Checked radio '{label_text[:40]}...' -> {ans}")
                            break
                        except Exception:
                            r.click(timeout=1000)
                            break
                continue

            # D. Check for standalone checkbox (e.g. Consent, Terms)
            checkbox = f_container.locator("input[type='checkbox']").first
            if checkbox.count() > 0 and checkbox.is_visible(timeout=400):
                if not checkbox.is_checked():
                    try:
                        checkbox.check(timeout=1000)
                        logging.info(f"Checked consent checkbox: '{label_text[:40]}...'")
                    except Exception:
                        checkbox.click(timeout=1000)
                continue

            # E. Check for Textarea / Open-ended text
            textarea = f_container.locator("textarea").first
            if textarea.count() > 0 and textarea.is_visible(timeout=400):
                curr = (textarea.input_value() or "").strip()
                if not curr:
                    ans = answer_custom_question(label_text, config)
                    textarea.fill(ans, timeout=2000)
                    logging.info(f"Filled textarea '{label_text[:40]}...' -> {ans[:50]}...")
                continue

            # F. Check for single-line text input
            text_inp = f_container.locator("input[type='text'], input[type='number'], input[type='url']").first
            if text_inp.count() > 0 and text_inp.is_visible(timeout=400):
                curr = (text_inp.input_value() or "").strip()
                if not curr:
                    ans = answer_custom_question(label_text, config)
                    text_inp.fill(ans, timeout=2000)
                    logging.info(f"Filled text input '{label_text[:40]}...' -> {ans}")
                continue

        except Exception as e:
            logging.debug(f"Field processing encountered non-fatal error: {e}")

    # 5. Handle Voluntary Demographics / EEO if present
    eeo_dropdowns = target.locator("select[id*='gender' i], select[id*='race' i], select[id*='veteran' i], select[id*='disability' i]").all()
    for eeo_sel in eeo_dropdowns:
        try:
            opts = [o.inner_text().strip() for o in eeo_sel.locator("option").all()]
            for opt in opts:
                if any(k in opt.lower() for k in ("decline", "do not wish", "male", "i am not a protected veteran", "no, i do not have a disability")):
                    eeo_sel.select_option(label=opt, timeout=1000)
                    break
        except Exception:
            pass

    return True


def apply_to_greenhouse_job(browser_context, job_info, config, dry_run=False, review_mode=False):
    """
    Open Greenhouse job URL, solve application form, and submit application.
    """
    url = job_info.get("url")
    title = job_info.get("title", "Software Role")
    company = job_info.get("company", "Tech Company")
    job_id = str(job_info.get("id", ""))

    logging.info(f"Navigating to {company} — {title} ({url})")
    page = browser_context.new_page()

    try:
        page.goto(url, wait_until="networkidle", timeout=35000)
    except PlaywrightTimeoutError:
        logging.warning("Page load timed out waiting for networkidle, proceeding with DOM content...")
    except Exception as e:
        logging.warning(f"Could not load job URL {url}: {e}")
        page.close()
        return False

    human_delay(1.5, 3.0)

    # Detect form target (page or iframe)
    target = get_greenhouse_target(page)

    # Verify form exists
    form_present = target.locator("form#application_form, form#application, form, #first_name").first.count() > 0
    if not form_present:
        logging.warning(f"No Greenhouse application form detected at {url}. Might be closed or external.")
        page.close()
        return False

    # Ensure Master Resume PDF is freshly generated
    resume_pdf_path = naukri_agent.ensure_master_resume_pdf(config)

    # Solve and fill the form
    solve_greenhouse_form(page, target, config, resume_pdf_path, dry_run=dry_run)
    human_delay(1.5, 2.5)

    # A. Dry-Run Mode: Save screenshot and skip submit
    if dry_run:
        logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
        screenshot_path = os.path.join(logs_dir, f"greenhouse_dry_run_{company}_{job_id}.png")
        try:
            page.screenshot(path=screenshot_path, full_page=True)
            logging.info(f"[DRY-RUN] Form filled successfully! Screenshot saved -> {screenshot_path}")
        except Exception:
            pass
        page.close()
        return True

    # B. Review Mode: Pause for user verification
    if review_mode or config.get("greenhouse", {}).get("review_mode", False):
        notify_macos(
            "Greenhouse Review Ready",
            f"Form filled for {company} ({title}). Please review in browser and submit.",
            is_urgent=True
        )
        print(f"\n[Greenhouse Review] Application form for {company} — {title} is filled.")
        print("Review the browser window. Press Enter in terminal once submitted, or type 'skip' to abort:")
        user_choice = input(">> ").strip().lower()
        page.close()
        return user_choice != "skip"

    # C. Autonomous Submit Mode
    submit_selectors = [
        "input[type='submit']#submit_app",
        "button#submit_app",
        "input[type='submit'][value*='Submit' i]",
        "button:has-text('Submit Application')",
        "button:has-text('Submit application')",
        "button:has-text('Submit')"
    ]

    submitted = False
    for s_sel in submit_selectors:
        try:
            btn = target.locator(s_sel).first
            if btn.count() > 0 and btn.is_visible(timeout=1500):
                logging.info(f"Clicking final submit button ({s_sel})...")
                btn.click(timeout=4000)
                submitted = True
                break
        except Exception as e:
            logging.debug(f"Submit button {s_sel} failed: {e}")

    if not submitted:
        logging.warning("Could not locate final submit button on Greenhouse form.")
        logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
        screenshot_path = os.path.join(logs_dir, f"greenhouse_stuck_{company}_{job_id}.png")
        try:
            page.screenshot(path=screenshot_path)
        except Exception:
            pass
        notifier.notify_stuck(
            platform="Greenhouse ATS",
            company=company,
            role=title,
            reason="Could not locate final submit button or validation blocked submission",
            url=url,
            screenshot_path=screenshot_path
        )
        page.close()
        return False

    # Wait for submission confirmation
    human_delay(3.5, 6.0)
    success = False
    try:
        conf_text = (page.inner_text("body", timeout=5000) or "").lower()
        if any(w in conf_text for w in ("thank you", "application submitted", "received your application", "successfully applied")):
            success = True
    except Exception:
        success = True  # If page redirected, treat as success

    if success:
        keyword_tracker.record_query_run(
            platform=f"Greenhouse ({company})",
            query=title,
            scanned=1,
            matched=1,
            applied=1
        )

    page.close()
    return success


# ---------------------------------------------------------------------------
# CLI Command & Batch Runner
# ---------------------------------------------------------------------------

def run_greenhouse_daily_sweep(config, max_applies=None, dry_run=False, force_headed=False, review_mode=False):
    """
    Execute daily Greenhouse application cycle across configured target boards.
    """
    gh_cfg = config.get("greenhouse", {})
    if not gh_cfg.get("enabled", True):
        logging.info("Greenhouse automation is disabled in config.json.")
        return

    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_greenhouse_ledger(logs_dir)

    daily_limit = gh_cfg.get("daily_apply_limit", 25)
    run_limit = max_applies or gh_cfg.get("apply_limit_per_run", 10)

    applies_today = count_applies_today(ledger)
    remaining_daily = max(0, daily_limit - applies_today)
    allowed_this_run = min(run_limit, remaining_daily)

    logging.info(f"Today's Greenhouse Applies: {applies_today}/{daily_limit} (Allowed this run: {allowed_this_run})")
    if allowed_this_run <= 0:
        logging.info("Daily Greenhouse application cap reached. Exiting cycle.")
        return

    # Discover jobs via public API
    discovered = search_greenhouse_jobs(config, ledger, max_jobs=allowed_this_run * 2)
    if not discovered:
        logging.info("No new matching Greenhouse jobs found at this time.")
        return

    with sync_playwright() as playwright:
        browser_context = launch_greenhouse_browser(playwright, config, force_headed=force_headed)
        applied_count = 0

        try:
            for job in discovered:
                if applied_count >= allowed_this_run:
                    break

                job_id = job.get("id")
                job_url = job.get("url")
                company = job.get("company")
                title = job.get("title")

                logging.info(f"[{applied_count + 1}/{allowed_this_run}] Processing {company} — {title}")
                success = apply_to_greenhouse_job(
                    browser_context, job, config, dry_run=dry_run, review_mode=review_mode
                )

                if success:
                    applied_count += 1
                    if not dry_run:
                        ledger["applied_jobs"][job_url] = {
                            "job_id": job_id,
                            "company": company,
                            "title": title,
                            "location": job.get("location"),
                            "url": job_url,
                            "status": "APPLIED",
                            "applied_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                        }
                        ledger["last_run_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                        save_greenhouse_ledger(logs_dir, ledger)

                        notify_macos(
                            "Greenhouse Application Sent!",
                            f"Applied to {company} for {title}.",
                            is_urgent=True
                        )

                human_delay(3.0, 6.0)

        finally:
            browser_context.close()

    logging.info(f"Completed Greenhouse cycle. Applied to {applied_count} jobs.")


def apply_single_url(url, config, dry_run=False, force_headed=False, review_mode=False):
    """
    Directly apply to a specific Greenhouse job link provided via CLI or Dropzone.
    """
    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_greenhouse_ledger(logs_dir)

    job_info = {
        "url": url,
        "title": "Direct Greenhouse Application",
        "company": "Company",
        "id": re.search(r'jobs/(\d+)', url).group(1) if re.search(r'jobs/(\d+)', url) else str(int(time.time()))
    }

    with sync_playwright() as playwright:
        browser_context = launch_greenhouse_browser(playwright, config, force_headed=force_headed)
        try:
            success = apply_to_greenhouse_job(
                browser_context, job_info, config, dry_run=dry_run, review_mode=review_mode
            )
            if success and not dry_run:
                ledger["applied_jobs"][url] = {
                    "job_id": job_info["id"],
                    "company": job_info["company"],
                    "title": job_info["title"],
                    "url": url,
                    "status": "APPLIED",
                    "applied_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                }
                save_greenhouse_ledger(logs_dir, ledger)
                logging.info(f"Successfully recorded application for {url}")
        finally:
            browser_context.close()


def print_status(config):
    """Print current ledger and daily metrics for Greenhouse applications."""
    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_greenhouse_ledger(logs_dir)
    today_count = count_applies_today(ledger)
    total_count = len(ledger.get("applied_jobs", {}))
    daily_limit = config.get("greenhouse", {}).get("daily_apply_limit", 25)

    print("\n" + "=" * 55)
    print("        GREENHOUSE AUTOMATION ENGINE STATUS")
    print("=" * 55)
    print(f" • Last Run:           {ledger.get('last_run_at') or 'Never'}")
    print(f" • Applies Today:      {today_count} / {daily_limit} (Daily Limit)")
    print(f" • Total Tracked:      {total_count} Applications")
    print(f" • Target Boards:      {len(config.get('greenhouse', {}).get('target_boards', []))} Companies Configured")
    print("=" * 55 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Greenhouse Automated Job Discovery & Form Solver")
    parser.add_argument("--url", type=str, help="Apply directly to a single Greenhouse job URL")
    parser.add_argument("--board", type=str, help="Target a specific company board (e.g. cockroachlabs, stripe)")
    parser.add_argument("--limit", type=int, help="Override maximum applications for this run")
    parser.add_argument("--dry-run", action="store_true", help="Fill form and save screenshot without submitting")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser window")
    parser.add_argument("--review", action="store_true", help="Fill form and pause for manual confirmation")
    parser.add_argument("--status", action="store_true", help="Display current application status and metrics")
    args = parser.parse_args()

    config = email_agent.load_config()

    if args.status:
        print_status(config)
        return

    with MacKeepAwake():
        if args.url:
            apply_single_url(
                args.url, config,
                dry_run=args.dry_run,
                force_headed=args.headed,
                review_mode=args.review
            )
        elif args.board:
            # Override target boards to the specified one
            config["greenhouse"]["target_boards"] = [args.board]
            run_greenhouse_daily_sweep(
                config,
                max_applies=args.limit,
                dry_run=args.dry_run,
                force_headed=args.headed,
                review_mode=args.review
            )
        else:
            run_greenhouse_daily_sweep(
                config,
                max_applies=args.limit,
                dry_run=args.dry_run,
                force_headed=args.headed,
                review_mode=args.review
            )


if __name__ == "__main__":
    main()
