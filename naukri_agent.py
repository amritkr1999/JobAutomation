#!/usr/bin/env python3
"""
Naukri.com Daily Profile Refresher & AI Auto-Applier (with Recruiter Chatbot Q&A)
Integrates with agent.py, config.json, and local Ollama LLM.
"""

import os
import sys
import re
import json
import time
import random
import logging
import argparse
import subprocess
from datetime import datetime
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Import shared utilities from existing email/PDF agent
import agent as email_agent
import qa_memory

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')


def human_delay(min_sec=1.5, max_sec=3.8):
    """Sleep for a randomized human-like interval."""
    time.sleep(random.uniform(min_sec, max_sec))


import power_utils
from power_utils import MacKeepAwake


def notify_macos(title, message, is_urgent=False):
    """Display a non-blocking macOS notification banner respecting quiet_mode."""
    power_utils.notify_macos(title, message, is_urgent=is_urgent)


def load_naukri_ledger(logs_dir):
    """Load persistent ledger of applied Naukri jobs and profile refresh history."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'naukri_applied_jobs.json')
    default_data = {
        "last_profile_refresh_at": None,
        "profile_refresh_count": 0,
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
            logging.warning(f"[Naukri] Could not read naukri_applied_jobs.json: {e}")
    return default_data


def save_naukri_ledger(logs_dir, ledger):
    """Persist Naukri applied jobs and profile refresh state."""
    ledger_path = os.path.join(logs_dir, 'naukri_applied_jobs.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"[Naukri] Could not save naukri_applied_jobs.json: {e}")


def count_applies_today(ledger):
    """Count how many Naukri jobs were successfully applied to today (YYYY-MM-DD)."""
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    applied = ledger.get("applied_jobs", {})
    count = 0
    for _, info in applied.items():
        if not isinstance(info, dict):
            continue
        status = info.get("status", "")
        ts = str(info.get("applied_at", ""))
        if status == "APPLIED" and ts.startswith(today_prefix):
            count += 1
    return count


def ensure_master_resume_pdf(config):
    """Generate a fresh Master Resume PDF using agent.py so Naukri gets an updated file timestamp."""
    paths = config.get('paths', {})
    output_dir = os.path.expanduser(paths.get('output', '~/Documents/JobAutomation/output'))
    os.makedirs(output_dir, exist_ok=True)
    master_pdf_path = os.path.join(output_dir, 'AMRIT_KUMAR_Resume.pdf')

    profile = config.get('profile', {})
    master_jd_info = {
        "company_name": "",
        "job_title": "Full Stack Developer",
        "key_requirements": [
            "Java 21", "Spring Boot 3", "Microservices", "React 19",
            "Python FastAPI", "PostgreSQL", "AWS", "System Design"
        ]
    }
    logging.info(f"[Naukri] Generating fresh master resume PDF -> {master_pdf_path}")
    email_agent.create_tailored_pdf_resume(master_pdf_path, profile, master_jd_info, jd_text="")
    return master_pdf_path


def cleanup_stale_chrome_locks(profile_dir):
    """Remove stale Chromium SingletonLock files if no Chrome process is using this profile dir."""
    if not os.path.exists(profile_dir):
        return
    for lock_name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        lock_path = os.path.join(profile_dir, lock_name)
        if os.path.lexists(lock_path):
            try:
                os.remove(lock_path)
            except Exception:
                pass


def launch_naukri_browser(playwright, config, force_headed=False):
    """Launch a persistent Google Chrome context dedicated to Naukri automation."""
    naukri_cfg = config.get('naukri', {})
    profile_dir = os.path.expanduser(
        naukri_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/naukri')
    )
    os.makedirs(profile_dir, exist_ok=True)
    cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = naukri_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(naukri_cfg.get('headless', False))

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
    if os.environ.get("JOB_AUTO_OFFSCREEN") == "1" or naukri_cfg.get("offscreen", False):
        launch_kwargs["args"].append("--window-position=-3000,-3000")

    if os.path.exists(chrome_exec):
        launch_kwargs["executable_path"] = chrome_exec

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    return context


def is_logged_in_to_naukri(page):
    """Check whether the current Naukri page represents an authenticated user session."""
    url = (page.url or "").lower()
    if "nlogin/login" in url or "login" in url.split("?")[0]:
        return False

    # Look for logged-in indicators on mnjuser/homepage or mnjuser/profile
    logged_in_selectors = [
        ".nI-gNb-drawer",
        ".nI-gNb-icon-img",
        "#lazyResumeHead",
        ".view-profile-wrapper",
        ".user-details-inner",
        ".fullname",
        "a[href*='mnjuser/profile']",
        ".nI-gNb-sb__main"
    ]
    login_form_selectors = [
        "#usernameField",
        "input[placeholder*='Email ID / Username']",
        "input[placeholder*='Enter your active Email ID']",
        "#login_Layer"
    ]
    for sel in login_form_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                return False
        except Exception:
            pass

    for sel in logged_in_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return True
        except Exception:
            pass

    return "mnjuser" in url


def ensure_logged_in(page, interactive_wait_seconds=300):
    """
    Navigate to Naukri profile page and verify login.
    If not logged in, wait for the user to log in once in the opened Chrome window.
    """
    logging.info("[Naukri] Navigating to Naukri profile to verify session...")
    try:
        page.goto("https://www.naukri.com/mnjuser/profile", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logging.warning(f"[Naukri] Initial navigation warning: {e}")

    human_delay(2.5, 4.0)

    if is_logged_in_to_naukri(page):
        logging.info("[Naukri] Active Naukri session confirmed!")
        return True

    msg = "Please log in to your Naukri.com account in the opened Chrome window. Session will be saved automatically."
    logging.warning(f"[Naukri] Not logged in yet. {msg}")
    print(f"\n[ACTION REQUIRED] {msg}\nWaiting up to {interactive_wait_seconds} seconds for login...")
    notify_macos("Naukri Automation", "Please log in once in the opened Chrome window.")

    try:
        if "naukri.com" not in (page.url or ""):
            page.goto("https://www.naukri.com/nlogin/login", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    start_time = time.time()
    while time.time() - start_time < interactive_wait_seconds:
        time.sleep(3)
        try:
            if is_logged_in_to_naukri(page):
                logging.info("[Naukri] Login detected! Saving persistent session.")
                print("[SUCCESS] Naukri login detected and saved to persistent Chrome profile!")
                human_delay(2.0, 3.5)
                return True
        except Exception:
            pass

    logging.error("[Naukri] Timed out waiting for Naukri login.")
    return False


def dismiss_naukri_popups(page):
    """Dismiss common Naukri promo modals, notifications, or chat overlays on profile/search pages."""
    close_selectors = [
        ".crossIcon",
        "span.crossIcon",
        ".ltCont .cross",
        "div[class*='crossIcon']",
        "button[aria-label='Close']"
    ]
    for sel in close_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=400):
                loc.click(timeout=1000)
                human_delay(0.5, 1.0)
        except Exception:
            pass


def refresh_naukri_profile(page, config, ledger, logs_dir):
    """
    Daily Profile Refresher:
      1. Uploads freshly rendered Master Resume PDF to update 'Resume Updated' timestamp.
      2. Toggles a trailing period in Resume Headline to trigger Naukri's 'Profile Last Updated' boost.
    """
    naukri_cfg = config.get('naukri', {})
    refresh_cfg = naukri_cfg.get('profile_refresh', {})
    if not refresh_cfg.get('enabled', True):
        logging.info("[Naukri] Profile refresh is disabled in config.")
        return False

    logging.info("[Naukri] Starting Daily Profile Refresh on Naukri.com...")
    try:
        if "mnjuser/profile" not in (page.url or ""):
            page.goto("https://www.naukri.com/mnjuser/profile", wait_until="domcontentloaded", timeout=45000)
            human_delay(3.0, 4.5)
    except Exception as e:
        logging.error(f"[Naukri] Failed to load profile page: {e}")
        return False

    dismiss_naukri_popups(page)
    refreshed_any = False

    # 1. Upload fresh Master Resume PDF
    if refresh_cfg.get('upload_fresh_resume', True):
        try:
            master_pdf = ensure_master_resume_pdf(config)
            file_input_selectors = [
                "input#attachCV",
                "input[type='file'][id*='attachCV']",
                ".uploadresume input[type='file']",
                "input[type='file']"
            ]
            uploaded = False
            for sel in file_input_selectors:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.set_input_files(master_pdf, timeout=15000)
                    logging.info(f"[Naukri] Uploaded fresh master resume PDF via '{sel}'.")
                    human_delay(4.5, 6.5)
                    uploaded = True
                    refreshed_any = True
                    break
            if not uploaded:
                logging.warning("[Naukri] Could not locate file input (#attachCV) for resume upload.")
        except Exception as e:
            logging.warning(f"[Naukri] Resume upload step encountered an issue: {e}")

    # 2. Toggle Resume Headline Heartbeat (adds/removes trailing period)
    if refresh_cfg.get('toggle_headline_heartbeat', True):
        try:
            edit_selectors = [
                "#lazyResumeHead span.edit",
                ".resumeHeadline span.edit",
                "div.resumeHeadline .edit",
                "span.widgetTitle:has-text('Resume headline') ~ span.edit",
                "//span[contains(text(),'Resume headline')]/following-sibling::span[contains(@class,'edit')]"
            ]
            clicked_edit = False
            for sel in edit_selectors:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=1500):
                    loc.scroll_into_view_if_needed(timeout=3000)
                    human_delay(0.6, 1.2)
                    loc.click(timeout=5000)
                    clicked_edit = True
                    break

            if clicked_edit:
                human_delay(1.5, 2.5)
                textarea_selectors = [
                    "textarea#resumeHeadlineTxt",
                    "textarea[id*='resumeHeadline']",
                    "form[name='resumeHeadlineForm'] textarea",
                    ".resumeHeadlineEdit textarea"
                ]
                for t_sel in textarea_selectors:
                    t_loc = page.locator(t_sel).first
                    if t_loc.count() > 0 and t_loc.is_visible(timeout=2000):
                        current_text = (t_loc.input_value() or "").strip()
                        if not current_text:
                            current_text = refresh_cfg.get(
                                "default_headline",
                                "Java Full Stack Developer | 3+ YOE | Java 21, Spring Boot 3, Microservices, React 19, Python FastAPI, PostgreSQL, AWS | Immediate / 7 Days Joiner"
                            )
                        if current_text.endswith("."):
                            new_text = current_text[:-1].strip()
                        else:
                            new_text = (current_text[:248].rstrip() + ".")

                        t_loc.fill(new_text)
                        human_delay(0.8, 1.5)

                        save_selectors = [
                            "form[name='resumeHeadlineForm'] button[type='submit']",
                            ".resumeHeadlineEdit button[type='submit']",
                            "button.btn-dark-ot[type='submit']",
                            "button:has-text('Save')"
                        ]
                        for s_sel in save_selectors:
                            s_loc = page.locator(s_sel).first
                            if s_loc.count() > 0 and s_loc.is_visible(timeout=1500):
                                s_loc.click(timeout=5000)
                                logging.info(f"[Naukri] Updated Resume Headline heartbeat -> '{new_text[:60]}...'")
                                human_delay(2.5, 4.0)
                                refreshed_any = True
                                break
                        break
            else:
                logging.warning("[Naukri] Could not find Resume Headline edit button.")
        except Exception as e:
            logging.warning(f"[Naukri] Resume Headline heartbeat step warning: {e}")

    # 3. Fallback: Key Skills quick open & save if neither step above succeeded
    if not refreshed_any:
        try:
            ks_loc = page.locator("#lazyKeySkills span.edit, .keySkills span.edit").first
            if ks_loc.count() > 0 and ks_loc.is_visible(timeout=1500):
                ks_loc.click(timeout=4000)
                human_delay(1.5, 2.5)
                save_btn = page.locator("#saveKeySkills, button:has-text('Save')").first
                if save_btn.count() > 0 and save_btn.is_visible(timeout=1500):
                    save_btn.click(timeout=4000)
                    logging.info("[Naukri] Refreshed profile via Key Skills Save fallback.")
                    human_delay(2.0, 3.0)
                    refreshed_any = True
        except Exception as e:
            logging.warning(f"[Naukri] Key Skills fallback warning: {e}")

    if refreshed_any:
        ledger["last_profile_refresh_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ledger["profile_refresh_count"] = int(ledger.get("profile_refresh_count", 0)) + 1
        save_naukri_ledger(logs_dir, ledger)
        logging.info("[Naukri] Daily Profile Refresh completed successfully!")
    else:
        logging.warning("[Naukri] Profile refresh finished without confirming an update.")

    return refreshed_any


def resolve_chatbot_answer(question_text, options, config):
    """
    Determine the right answer for a Naukri recruiter chatbot question.
    Uses universal QA memory first, deterministic rules from config['naukri']['qa_bank'], then local Ollama.
    """
    # 0. Check universal QA memory bank first
    saved = qa_memory.recall_answer(question_text, available_options=options)
    if saved:
        logging.info(f"[Naukri QA Memory] Auto-recalled learned answer for '{question_text[:40]}...' -> '{saved}'")
        return saved

    q = (question_text or "").strip().lower()
    qa = config.get("naukri", {}).get("qa_bank", {})
    skill_map = qa.get("skill_experience_years", {})

    # Helper to match the best option from radio/checkbox options
    def match_option(preferred_keywords):
        if not options:
            return None
        for kw in preferred_keywords:
            kw_low = kw.lower()
            for opt in options:
                if kw_low == opt.strip().lower():
                    return opt
            for opt in options:
                if kw_low in opt.strip().lower():
                    return opt
        return None

    # 1. Notice Period / Joining Time
    if any(k in q for k in ("notice period", "how soon can you join", "joining time", "days to join", "early joiner", "immediate joiner", "serving notice")):
        if "serving" in q and ("yes" in [o.lower() for o in options] or not options):
            opt = match_option(["yes", "immediate", "15 days", "less than 15"])
            return opt or qa.get("is_serving_notice", "Yes")
        opt = match_option(["immediate", "0 - 15", "15 days or less", "less than 15", "7 days", "1 month", "30 days"])
        if opt:
            return opt
        if "month" in q:
            return "0"
        return qa.get("notice_period_days", "7")

    # 2. Current CTC / Present Salary
    if any(k in q for k in ("current ctc", "present ctc", "current salary", "current annual", "fixed ctc", "current package")):
        if options:
            opt = match_option(["12", "10 - 15", "10-15", "11 - 15", "8 - 12", "12 - 15"])
            if opt:
                return opt
        if "rupee" in q or "inr" in q or "eg: 500000" in q or "00000" in q:
            return qa.get("current_ctc_inr", "1200000")
        return qa.get("current_ctc_lpa", "12")

    # 3. Expected CTC / Expected Salary
    if any(k in q for k in ("expected ctc", "ectc", "expected salary", "expected annual", "salary expectation", "expected package")):
        if options:
            opt = match_option(["15", "12 - 16", "14 - 18", "15 - 20", "12 - 15", "10 - 15"])
            if opt:
                return opt
        if "rupee" in q or "inr" in q or "eg: 500000" in q or "00000" in q:
            return qa.get("expected_ctc_inr", "1500000")
        return qa.get("expected_ctc_lpa", "15")

    # 4. Skill-specific experience (e.g. "How many years of experience do you have in Spring Boot?")
    if any(k in q for k in ("experience", "years", "exp ", "how long", "hands-on", "proficiency")):
        for skill_name, yrs in sorted(skill_map.items(), key=lambda x: -len(x[0])):
            if skill_name.lower() in q:
                if options:
                    opt = match_option([yrs, "3", "2 - 4", "3 - 5", "2+", "3+", "yes"])
                    if opt:
                        return opt
                return str(yrs)

        # Total / Relevant Experience
        if any(k in q for k in ("total experience", "overall experience", "relevant experience", "years of experience", "work experience")):
            if options:
                opt = match_option(["3", "3.5", "3 - 5", "2 - 4", "3-5", "2-5"])
                if opt:
                    return opt
            return qa.get("relevant_experience_years", "3")

    # 5. Location / Relocation / Hybrid / Work from Office / Remote
    if any(k in q for k in ("relocate", "relocation", "work from office", "wfo", "hybrid", "onsite", "comfortable working", "shift", "rotational", "available for", "join immediately")):
        opt = match_option(["yes", "immediate", "comfortable", "agree"])
        if opt:
            return opt
        return "Yes"

    if any(k in q for k in ("current location", "currently located", "city are you", "base location", "preferred location")):
        if options:
            opt = match_option(["bangalore", "bengaluru", "hyderabad", "pune", "noida", "gurgaon", "delhi", "mumbai", "ahmedabad", "remote", "anywhere", "india", "yes"])
            if opt:
                return opt
        return "Bangalore"

    # 6. Education / Qualification / Degree / CGPA
    if any(k in q for k in ("qualification", "degree", "education", "b.tech", "b.e", "graduate", "graduation")):
        if options:
            opt = match_option(["b.tech", "b.e", "bachelor", "graduate", "computer science", "yes", "2023"])
            if opt:
                return opt
        if "year" in q or "passout" in q or "passing" in q:
            return qa.get("graduation_year", "2023")
        return "B.Tech in Computer Science"

    if any(k in q for k in ("percentage", "cgpa", "marks")):
        return qa.get("percentage_cgpa", "8.2")

    if any(k in q for k in ("dob", "date of birth", "birth date")):
        if options:
            opt = match_option(["12/06/1999", "12 June 1999", "1999"])
            if opt:
                return opt
        return qa.get("date_of_birth", "12/06/1999")

    # 7. Generic Yes/No radio questions ("Do you have...", "Are you...", "Have you worked...")
    if options and len(options) <= 4:
        opt = match_option(["yes", "true", "immediate"])
        if opt:
            return opt

    # 8. Ollama Fallback for custom open-ended or multiple-choice questions
    try:
        opts_str = f"Available Options: {options}\n" if options else "Input Type: Short Text / Number\n"
        prompt = f"""You are answering a job application screening chatbot question on behalf of candidate Amrit Kumar.
Candidate Profile:
- Role: Java Full Stack Developer (3.5 years total experience, 3 years relevant experience)
- Skills: Java 21, Spring Boot 3, Microservices, Hibernate, JPA, React 19, TypeScript, Python FastAPI, PostgreSQL, MySQL, MongoDB, Redis, AWS, Docker, Kafka, Git (2-3 years experience in all)
- Current CTC: 12 LPA (1200000 INR)
- Expected CTC: 15 LPA (1500000 INR)
- Notice Period: 7 days (Immediate joiner)
- Date of Birth (DOB): 12 June 1999 (12/06/1999)
- Education: B.Tech Computer Science (2023), 8.2 CGPA
- Willing to relocate / work in office / hybrid / shifts: Yes

Question: "{question_text}"
{opts_str}
Rules:
1. If Available Options are provided, output ONLY the exact text of the single best matching option from the list.
2. If no options are provided and the question asks for years/numbers, output ONLY the number (e.g. 3).
3. Otherwise output a concise, factual 1-5 word answer. No explanations or markdown."""
        llm_ans = email_agent.query_ollama(config, prompt)
        if llm_ans:
            cleaned = llm_ans.strip().strip('"').strip("'").splitlines()[0].strip()
            if options:
                matched = match_option([cleaned])
                if matched:
                    return matched
                return options[0]
            return cleaned
    except Exception as e:
        logging.warning(f"[Naukri Chatbot] Ollama fallback error: {e}")

    if options:
        return options[0]
    return "3"


def handle_naukri_chatbot(page, config):
    """
    Detect and complete Naukri's right-side recruiter screening chatbot drawer if it appears after clicking Apply.
    Returns True if the application reaches 'Applied' / completion state.
    """
    human_delay(2.0, 3.2)

    # Check if already confirmed applied without a chatbot
    applied_confirm_selectors = [
        "span:has-text('Applied')",
        "#already-applied",
        ".apply-status-header:has-text('Applied')",
        "div:has-text('You have successfully applied')",
        "div:has-text('successfully applied to')"
    ]

    drawer_selectors = [
        ".chatbot_DrawerContentWrapper",
        ".chatbot_MessageContainer",
        "div[id^='chatList_']",
        ".ssrc__question"
    ]

    has_drawer = False
    for sel in drawer_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=1200):
                has_drawer = True
                break
        except Exception:
            pass

    if not has_drawer:
        for sel in applied_confirm_selectors:
            try:
                if page.locator(sel).first.is_visible(timeout=1000):
                    return True
            except Exception:
                pass
        return True

    logging.info("[Naukri Chatbot] Recruiter screening chatbot detected. Answering questions...")
    last_question = ""
    repeat_count = 0

    for step in range(15):
        human_delay(1.2, 2.2)

        # Check if chatbot closed or application completed
        still_open = False
        for sel in drawer_selectors:
            try:
                if page.locator(sel).first.is_visible(timeout=600):
                    still_open = True
                    break
            except Exception:
                pass
        if not still_open:
            logging.info("[Naukri Chatbot] Chatbot drawer closed — application submitted!")
            return True

        # Extract the latest bot question
        question_text = ""
        q_selectors = [
            "li.botItem .botMsg span",
            "li.botItem div.botMsg",
            ".chatbot_MessageContainer .botMsg",
            ".ssrc__question"
        ]
        for q_sel in q_selectors:
            try:
                q_locs = page.locator(q_sel)
                cnt = q_locs.count()
                if cnt > 0:
                    question_text = (q_locs.nth(cnt - 1).inner_text(timeout=1000) or "").strip()
                    if question_text:
                        break
            except Exception:
                pass

        if not question_text:
            # Check if there is a final Submit/Save button
            try:
                save_btn = page.locator(".sendMsg, .sendMsgbtn, button:has-text('Save'), button:has-text('Submit')").first
                if save_btn.count() > 0 and save_btn.is_visible(timeout=600):
                    save_btn.click(timeout=3000)
                    continue
            except Exception:
                pass
            break

        # Detect final completion message from bot (not a question)
        q_lower = question_text.lower()
        if any(done_phrase in q_lower for done_phrase in (
            "thank you for your response",
            "thank you for applying",
            "responses have been recorded",
            "successfully applied",
            "application has been submitted"
        )):
            logging.info("[Naukri Chatbot] Chatbot confirmed responses recorded — application submitted!")
            return True

        if question_text == last_question:
            repeat_count += 1
            if repeat_count >= 3:
                logging.warning(f"[Naukri Chatbot] Stuck on repeated question: '{question_text[:80]}'. Breaking.")
                return False
        else:
            last_question = question_text
            repeat_count = 0

        # Gather radio/checkbox options if present
        option_labels = []
        opt_selectors = [
            ".ssrc__radio-btn-container label",
            ".singleselect-radiobutton label",
            ".multiselectcheckboxes label",
            ".chatbot_DrawerContentWrapper label"
        ]
        active_opt_selector = None
        for o_sel in opt_selectors:
            try:
                locs = page.locator(o_sel)
                cnt = locs.count()
                if cnt > 0:
                    visible_opts = []
                    for i in range(cnt):
                        if locs.nth(i).is_visible(timeout=300):
                            txt = (locs.nth(i).inner_text(timeout=500) or "").strip()
                            if txt:
                                visible_opts.append(txt)
                    if visible_opts:
                        option_labels = visible_opts
                        active_opt_selector = o_sel
                        break
            except Exception:
                pass

        answer = resolve_chatbot_answer(question_text, option_labels, config)
        logging.info(f"[Naukri Chatbot] Q{step+1}: '{question_text[:70]}' -> Answer: '{answer}'")

        answered = False
        # Special case: Naukri DOB 3-field widget (DD / MM / YYYY) or date input
        if any(k in q_lower for k in ("dob", "date of birth", "birth date")):
            qa = config.get("naukri", {}).get("qa_bank", {})
            dob_d = qa.get("dob_day", "12")
            dob_m = qa.get("dob_month", "06")
            dob_y = qa.get("dob_year", "1999")
            try:
                dd_inp = page.locator("input[placeholder*='DD' i], input[name*='day' i]").first
                mm_inp = page.locator("input[placeholder*='MM' i], input[name*='month' i]").first
                yy_inp = page.locator("input[placeholder*='YYYY' i], input[placeholder*='YY' i], input[name*='year' i]").first
                if dd_inp.count() > 0 and mm_inp.count() > 0 and yy_inp.count() > 0 and dd_inp.is_visible(timeout=400):
                    dd_inp.fill(dob_d, timeout=2000)
                    mm_inp.fill(dob_m, timeout=2000)
                    yy_inp.fill(dob_y, timeout=2000)
                    answered = True
            except Exception:
                pass

        # 1. If radio/checkbox options exist, click the matching label
        if not answered and option_labels and active_opt_selector:
            try:
                locs = page.locator(active_opt_selector)
                for i in range(locs.count()):
                    item = locs.nth(i)
                    if not item.is_visible(timeout=300):
                        continue
                    txt = (item.inner_text(timeout=500) or "").strip()
                    if txt.lower() == answer.lower() or answer.lower() in txt.lower():
                        item.click(timeout=3000)
                        answered = True
                        break
                if not answered and locs.count() > 0:
                    locs.first.click(timeout=3000)
                    answered = True
            except Exception as e:
                logging.warning(f"[Naukri Chatbot] Option click warning: {e}")

        # 2. If text/number input or contenteditable div exists, fill it
        if not answered:
            input_selectors = [
                ".chatbot_DrawerContentWrapper div.textArea[contenteditable='true']",
                ".chatbot_DrawerContentWrapper input[type='text']",
                ".chatbot_DrawerContentWrapper input[type='number']",
                ".chatbot_DrawerContentWrapper textarea",
                "div.textArea[contenteditable='true']",
                "#userInput__chatList"
            ]
            for i_sel in input_selectors:
                try:
                    inp = page.locator(i_sel).first
                    if inp.count() > 0 and inp.is_visible(timeout=500):
                        inp.click(timeout=2000)
                        inp.fill(str(answer), timeout=3000)
                        answered = True
                        break
                except Exception:
                    pass

        human_delay(0.6, 1.2)

        # 3. Click Save / Next / Send button
        send_selectors = [
            "div.sendMsg",
            ".sendMsgbtn",
            "button.sendMsg",
            ".chatbot_DrawerContentWrapper button:has-text('Save')",
            ".chatbot_DrawerContentWrapper button:has-text('Next')",
            ".chatbot_DrawerContentWrapper button:has-text('Submit')",
            "button:has-text('Save')"
        ]
        clicked_send = False
        for s_sel in send_selectors:
            try:
                btn = page.locator(s_sel).first
                if btn.count() > 0 and btn.is_visible(timeout=500):
                    btn.click(timeout=3000)
                    clicked_send = True
                    break
            except Exception:
                pass

        if not clicked_send:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass

    return True


def parse_min_experience(exp_text):
    """Extract minimum years of experience from strings like '3-6 Yrs' or '2 - 5 years'."""
    if not exp_text:
        return 0
    m = re.search(r'(\d+)\s*(?:-\s*\d+)?\s*(?:yrs|years|yr)', exp_text.lower())
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


def extract_job_id_from_url(url):
    """Extract Naukri numeric job ID from URL or return normalized URL."""
    if not url:
        return ""
    m = re.search(r'-(\d{9,14})(?:\?|$|/)', url)
    if m:
        return m.group(1)
    return url.split("?")[0].strip()


def bridge_hr_emails_to_dropzone(jd_text, title, company, job_id, config):
    """
    If a Naukri JD contains recruiter email addresses, save a job post file into Dropzone
    so agent.py automatically dispatches a tailored Resume & Cover Letter cold email too.
    """
    search_cfg = config.get("naukri", {}).get("search", {})
    if not search_cfg.get("extract_hr_emails_to_dropzone", True):
        return []

    sender_email = config.get("email", {}).get("sender_email", "")
    found_emails = email_agent.extract_valid_emails("", jd_text, sender_email)
    if not found_emails:
        # Check if JD contains an external Greenhouse or Workday ATS link
        import ats_bridge
        ats_type, ats_url = ats_bridge.find_ats_url_in_text(jd_text, resolve_shorteners=True)
        if ats_type and ats_url:
            ats_bridge.queue_ats_job_to_dropzone(
                ats_type, ats_url, title, company, source="Naukri_JD", jd_text=jd_text, config=config
            )
            return [f"{ats_type.upper()}:{ats_url}"]
        return []

    dropzone_dir = email_agent.get_dropzone_path(config)
    os.makedirs(dropzone_dir, exist_ok=True)
    safe_company = re.sub(r'[^\w\-]', '_', (company or "Company"))[:30]
    filename = f"Naukri_JD_{safe_company}_{job_id}.txt"
    file_path = os.path.join(dropzone_dir, filename)

    if not os.path.exists(file_path):
        try:
            content = (
                f"Company: {company}\n"
                f"Role: {title}\n"
                f"Recruiter Email: {', '.join(found_emails)}\n\n"
                f"Job Description:\n{jd_text}\n"
            )
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logging.info(f"[Naukri -> Email Bridge] Extracted HR email(s) {found_emails} and queued {filename} in Dropzone!")
        except Exception as e:
            logging.warning(f"[Naukri -> Email Bridge] Failed to write dropzone file: {e}")

    return found_emails


def collect_job_cards_on_page(page):
    """Extract job listings (title, company, url, experience) from current Naukri search/recommended page."""
    jobs = []
    card_selectors = [
        "div.srp-jobtuple-wrapper",
        "article.jobTuple",
        "div.cust-job-tuple",
        "div.tuple"
    ]
    for sel in card_selectors:
        cards = page.locator(sel)
        cnt = cards.count()
        if cnt == 0:
            continue
        for i in range(cnt):
            card = cards.nth(i)
            try:
                title_loc = card.locator("a.title, a.subTitle, .title a").first
                if title_loc.count() == 0:
                    continue
                title = (title_loc.inner_text(timeout=800) or "").strip()
                href = title_loc.get_attribute("href", timeout=800) or ""
                if not href or not title:
                    continue
                if href.startswith("/"):
                    href = f"https://www.naukri.com{href}"

                comp_loc = card.locator("a.comp-name, .comp-name, a.subTitle").first
                company = (comp_loc.inner_text(timeout=500) or "").strip() if comp_loc.count() > 0 else ""

                exp_loc = card.locator("span.expwdth, .exp, li.experience").first
                exp_text = (exp_loc.inner_text(timeout=500) or "").strip() if exp_loc.count() > 0 else ""

                jobs.append({
                    "title": title,
                    "company": company,
                    "url": href,
                    "exp_text": exp_text,
                    "job_id": extract_job_id_from_url(href)
                })
            except Exception:
                continue
        if jobs:
            break
    return jobs


def build_naukri_search_url(query, exp_years=3, freshness_days=7):
    """Build a clean Naukri search URL for a given role query."""
    slug = re.sub(r'[^a-z0-9]+', '-', query.lower()).strip('-')
    q_encoded = quote_plus(query)
    return f"https://www.naukri.com/{slug}-jobs?k={q_encoded}&experience={exp_years}&jobAge={freshness_days}"


def run_naukri_auto_apply(context, page, config, ledger, logs_dir, max_applies_override=None):
    """
    Search Naukri jobs across configured queries & recommended jobs, filter out mismatches,
    extract any HR emails to Dropzone, and auto-apply (including answering recruiter chatbot questions).
    """
    naukri_cfg = config.get("naukri", {})
    search_cfg = naukri_cfg.get("search", {})
    daily_limit = int(naukri_cfg.get("daily_apply_limit", 100))
    run_limit = int(naukri_cfg.get("apply_limit_per_run", 50))
    session_limit = int(max_applies_override) if max_applies_override is not None else run_limit
    excluded_keywords = [k.lower() for k in search_cfg.get("excluded_title_keywords", [])]
    max_min_exp = int(search_cfg.get("max_min_experience_allowed", 5))
    skip_external = bool(search_cfg.get("skip_external_company_sites", True))

    applies_today = count_applies_today(ledger)
    logging.info(
        f"[Naukri] Auto-Apply status today: {applies_today}/{daily_limit} jobs applied so far "
        f"(Session limit: {session_limit})."
    )
    if applies_today >= daily_limit and max_applies_override is None:
        logging.info("[Naukri] Reached daily apply limit. Skipping further applications for today.")
        return 0

    candidate_jobs = []
    seen_ids = set()

    # 1. Collect from Recommended Jobs first (highest relevance & 1-click apply rate)
    if search_cfg.get("include_recommended_jobs", True):
        try:
            logging.info("[Naukri] Scanning Naukri Recommended Jobs...")
            page.goto("https://www.naukri.com/mnjuser/recommendedjobs", wait_until="domcontentloaded", timeout=40000)
            human_delay(3.0, 4.5)
            dismiss_naukri_popups(page)
            rec_jobs = collect_job_cards_on_page(page)
            logging.info(f"[Naukri] Found {len(rec_jobs)} recommended job card(s).")
            for jb in rec_jobs:
                jid = jb["job_id"]
                if jid and jid not in seen_ids:
                    seen_ids.add(jid)
                    candidate_jobs.append(jb)
        except Exception as e:
            logging.warning(f"[Naukri] Recommended jobs scan warning: {e}")

    # 2. Collect from each configured Search Query
    queries = search_cfg.get("queries", ["Java Full Stack Developer"])
    exp_years = int(search_cfg.get("experience_years", 3))
    freshness = int(search_cfg.get("freshness_days", 7))

    for query in queries:
        search_url = build_naukri_search_url(query, exp_years=exp_years, freshness_days=freshness)
        logging.info(f"[Naukri] Searching query: '{query}' -> {search_url}")
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            human_delay(3.0, 4.5)
            dismiss_naukri_popups(page)
            found = collect_job_cards_on_page(page)
            logging.info(f"[Naukri] Found {len(found)} job card(s) for '{query}'.")
            for jb in found:
                jid = jb["job_id"]
                if jid and jid not in seen_ids:
                    seen_ids.add(jid)
                    candidate_jobs.append(jb)
        except Exception as e:
            logging.warning(f"[Naukri] Search query '{query}' warning: {e}")

    logging.info(f"[Naukri] Total unique job listings collected this run: {len(candidate_jobs)}")

    applied_this_run = 0
    applied_ledger = ledger.setdefault("applied_jobs", {})

    for jb in candidate_jobs:
        if applied_this_run >= session_limit:
            logging.info(f"[Naukri] Reached session apply limit ({session_limit}). Stopping auto-apply loop.")
            break
        if max_applies_override is None and (applies_today + applied_this_run) >= daily_limit:
            logging.info(f"[Naukri] Reached daily apply limit ({daily_limit}). Stopping auto-apply loop.")
            break

        jid = jb["job_id"]
        title = jb["title"]
        company = jb["company"]
        url = jb["url"]
        exp_text = jb["exp_text"]

        # Skip if already processed in ledger
        if jid in applied_ledger:
            continue

        # Filter excluded keywords in title
        title_low = title.lower()
        if any(bad_kw in title_low for bad_kw in excluded_keywords):
            logging.info(f"[Naukri] Skipping excluded role title: '{title}' ({company})")
            applied_ledger[jid] = {
                "title": title,
                "company": company,
                "url": url,
                "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "SKIPPED_EXCLUDED_TITLE"
            }
            continue

        # Filter excessive minimum experience
        min_exp = parse_min_experience(exp_text)
        if min_exp > max_min_exp:
            logging.info(f"[Naukri] Skipping senior requirement ({exp_text}): '{title}' at {company}")
            applied_ledger[jid] = {
                "title": title,
                "company": company,
                "url": url,
                "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": f"SKIPPED_EXP_{exp_text}"
            }
            continue

        # Open job detail page
        logging.info(f"[Naukri] Inspecting job: '{title}' at {company or 'Unknown'} ({exp_text or 'N/A'})")
        job_page = context.new_page()
        try:
            job_page.goto(url, wait_until="domcontentloaded", timeout=40000)
            human_delay(2.2, 3.8)

            # Extract JD text for HR email bridge
            jd_text = ""
            jd_selectors = [
                ".styles_JDC__dang-inner-html__h0K4t",
                "section.job-desc",
                ".job-desc",
                "div[class*='dang-inner-html']"
            ]
            for j_sel in jd_selectors:
                loc = job_page.locator(j_sel).first
                if loc.count() > 0:
                    jd_text = (loc.inner_text(timeout=1500) or "").strip()
                    if jd_text:
                        break

            hr_emails = []
            if jd_text:
                hr_emails = bridge_hr_emails_to_dropzone(jd_text, title, company, jid, config)

            # Check if already applied on Naukri
            if job_page.locator("#already-applied, span:has-text('Already Applied')").first.count() > 0:
                logging.info(f"[Naukri] Already applied to '{title}' at {company}.")
                applied_ledger[jid] = {
                    "title": title,
                    "company": company,
                    "url": url,
                    "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "ALREADY_APPLIED",
                    "hr_emails": hr_emails
                }
                save_naukri_ledger(logs_dir, ledger)
                continue

            # Check if external company site button
            ext_btn = job_page.locator("#company-site-button, button:has-text('Apply on company site'), a:has-text('Apply on company site')").first
            if ext_btn.count() > 0 and ext_btn.is_visible(timeout=800):
                # Inspect destination to see if it is a Greenhouse or Workday ATS link
                import ats_bridge
                ext_url = (ext_btn.get_attribute("href") or ext_btn.get_attribute("data-href") or "").strip()
                if not ext_url:
                    try:
                        parent_a = ext_btn.locator("xpath=..").first
                        if parent_a.count() > 0 and parent_a.get_attribute("href"):
                            ext_url = parent_a.get_attribute("href").strip()
                    except Exception:
                        pass

                ats_type, ats_url = None, None
                if ext_url:
                    ats_type, ats_url = ats_bridge.find_ats_url_in_text(ext_url, resolve_shorteners=True)
                if not ats_type and jd_text:
                    ats_type, ats_url = ats_bridge.find_ats_url_in_text(jd_text, resolve_shorteners=True)

                if ats_type and ats_url:
                    logging.info(f"[Naukri] 🎯 External company site leads to {ats_type.upper()}: {ats_url}. Queuing to Dropzone!")
                    ats_bridge.queue_ats_job_to_dropzone(
                        ats_type, ats_url, title, company, source="Naukri_Company_Site", jd_text=jd_text, config=config
                    )
                    applied_ledger[jid] = {
                        "title": title,
                        "company": company,
                        "url": url,
                        "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": f"QUEUED_{ats_type.upper()}_ATS",
                        "ats_url": ats_url
                    }
                    save_naukri_ledger(logs_dir, ledger)
                    continue

                if skip_external:
                    logging.info(f"[Naukri] Skipping unsupported external company site for '{title}' at {company}.")
                    applied_ledger[jid] = {
                        "title": title,
                        "company": company,
                        "url": url,
                        "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "SKIPPED_EXTERNAL_SITE",
                        "hr_emails": hr_emails
                    }
                    save_naukri_ledger(logs_dir, ledger)
                    continue

            # Find standard Naukri Apply button
            apply_btn = job_page.locator("#apply-button, button.apply-button, button:has-text('Apply')").first
            if apply_btn.count() == 0 or not apply_btn.is_visible(timeout=1500):
                logging.info(f"[Naukri] No direct Apply button found for '{title}' at {company}.")
                continue

            btn_text = (apply_btn.inner_text(timeout=800) or "").strip().lower()
            if "company site" in btn_text and skip_external:
                logging.info(f"[Naukri] Skipping external company site for '{title}' at {company}.")
                continue

            # Click Apply!
            apply_btn.click(timeout=5000)
            logging.info(f"[Naukri] Clicked Apply for '{title}' at {company}...")

            # Handle Recruiter Chatbot if it pops up
            success = handle_naukri_chatbot(job_page, config)
            status_str = "APPLIED" if success else "CHATBOT_INCOMPLETE"
            if success:
                applied_this_run += 1
                logging.info(
                    f"[Naukri] Successfully applied ({applies_today + applied_this_run}/{daily_limit} today): "
                    f"'{title}' at {company}"
                )

            applied_ledger[jid] = {
                "title": title,
                "company": company,
                "url": url,
                "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": status_str,
                "hr_emails": hr_emails
            }
            save_naukri_ledger(logs_dir, ledger)
            human_delay(2.5, 5.0)

        except PlaywrightTimeoutError:
            logging.warning(f"[Naukri] Timeout while processing job '{title}' ({url}).")
        except Exception as e:
            logging.warning(f"[Naukri] Error processing job '{title}': {e}")
        finally:
            try:
                job_page.close()
            except Exception:
                pass

    logging.info(f"[Naukri] Auto-Apply run finished. Applied to {applied_this_run} new job(s) in this session.")
    return applied_this_run


def run_naukri_pipeline(mode="all", max_applies=None, force_headed=False, interactive_login=True):
    """Main entry point for Naukri profile refresh and auto-apply pipeline."""
    import fcntl
    lock_path = "/tmp/naukri_automation_agent.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, IOError):
        print("Another Naukri Automation instance is already running. Skipping concurrent execution.")
        return False

    config = email_agent.load_config()
    paths = config.get('paths', {})
    logs_dir = os.path.expanduser(paths.get('logs', '~/Documents/JobAutomation/logs'))
    email_agent.setup_logging(logs_dir)

    naukri_cfg = config.get('naukri', {})
    if not naukri_cfg.get('enabled', True):
        logging.info("[Naukri] Naukri automation is disabled in config.json.")
        return False

    ledger = load_naukri_ledger(logs_dir)
    if mode in ("all", "apply"):
        ledger["last_daily_auto_run_date"] = datetime.now().strftime("%Y-%m-%d")
        save_naukri_ledger(logs_dir, ledger)

    with sync_playwright() as p:
        context = launch_naukri_browser(p, config, force_headed=force_headed or (mode == "login"))
        try:
            page = context.pages[0] if context.pages else context.new_page()

            wait_secs = 300 if (interactive_login or mode == "login") else 15
            if not ensure_logged_in(page, interactive_wait_seconds=wait_secs):
                logging.error("[Naukri] Cannot proceed without an authenticated Naukri session. Run: python3 naukri_agent.py --mode login")
                return False

            if mode == "login":
                logging.info("[Naukri] Login verification complete! Persistent profile is ready.")
                return True

            if mode in ("all", "refresh"):
                try:
                    refresh_naukri_profile(page, config, ledger, logs_dir)
                except Exception as e:
                    logging.error(f"[Naukri] Profile refresh encountered error: {e}", exc_info=True)

            if mode in ("all", "apply"):
                try:
                    run_naukri_auto_apply(context, page, config, ledger, logs_dir, max_applies_override=max_applies)
                except Exception as e:
                    logging.error(f"[Naukri] Auto apply encountered error: {e}", exc_info=True)

            ledger["last_daily_auto_run_date"] = datetime.now().strftime("%Y-%m-%d")
            save_naukri_ledger(logs_dir, ledger)
            return True

        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Naukri.com Daily Profile Refresher & Smart Auto-Applier")
    parser.add_argument(
        "--mode",
        choices=["all", "login", "refresh", "apply"],
        default="all",
        help="Execution mode: 'login' (one-time login setup), 'refresh' (profile update only), 'apply' (auto-apply only), or 'all' (both)."
    )
    parser.add_argument(
        "--max-applies",
        type=int,
        default=None,
        help="Override maximum number of jobs to apply in this run."
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Force visible Chrome window."
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="Run headed Chrome off-screen to avoid covering workspace."
    )
    args = parser.parse_args()
    if args.offscreen:
        os.environ["JOB_AUTO_OFFSCREEN"] = "1"
    with MacKeepAwake():
        run_naukri_pipeline(mode=args.mode, max_applies=args.max_applies, force_headed=args.headed)
