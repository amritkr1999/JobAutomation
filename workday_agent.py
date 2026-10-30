#!/usr/bin/env python3
"""
Workday Automated Job Discovery & Multi-Step Application Wizard Engine
Autonomous ATS agent for Amrit Kumar.
Supports:
  1. Automated Workday Career Portal Sweeping & Role Discovery
  2. Direct Workday Job URL Processing & Application
  3. Workday Candidate Account Creation & Authentication
  4. Multi-Step Wizard Autofill (Resume, Contact Info, Screening Questions, Disclosures)
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
from datetime import datetime
from urllib.parse import urlparse, urljoin

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
    format='%(asctime)s [%(levelname)s] [Workday] %(message)s',
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

def load_workday_ledger(logs_dir):
    """Load persistent ledger of applied Workday jobs."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'workday_applied_jobs.json')
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
            logging.warning(f"Could not read workday_applied_jobs.json: {e}")
    return default_data


def save_workday_ledger(logs_dir, ledger):
    """Persist updated ledger to workday_applied_jobs.json."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'workday_applied_jobs.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.warning(f"Could not save workday_applied_jobs.json: {e}")


def count_applies_today(ledger):
    """Count how many Workday applications were completed today (YYYY-MM-DD)."""
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
# Browser Initialization
# ---------------------------------------------------------------------------

def launch_workday_browser(playwright, config, force_headed=False):
    """Launch persistent Google Chrome context dedicated to Workday automation."""
    wd_cfg = config.get('workday', {})
    profile_dir = os.path.expanduser(
        wd_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/workday')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = wd_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(wd_cfg.get('headless', True))

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
# Workday Portal Discovery & Search Engine
# ---------------------------------------------------------------------------

def is_workday_job_matching(title, loc_text, wd_cfg):
    """Check if job title and location match candidate preferences."""
    t_lower = (title or "").lower().strip()
    if not t_lower:
        return False

    # 1. Excluded keywords
    excluded = [k.lower() for k in wd_cfg.get("excluded_title_keywords", [])]
    for ex in excluded:
        if ex and ex in t_lower:
            return False

    # 2. Queries / Target tech stack
    queries = [q.lower() for q in wd_cfg.get("queries", [])]
    target_stems = [
        "full stack", "fullstack", "software engineer", "software developer",
        "backend", "back-end", "java", "python", "spring boot", "spring",
        "react", "fastapi", "sde", "mts", "cloud"
    ]
    all_targets = set(queries + target_stems)
    if not any(t in t_lower for t in all_targets):
        return False

    # 3. Location filtering
    loc_lower = (loc_text or "").lower().strip()
    target_locs = [l.lower() for l in wd_cfg.get("locations", [])]
    if not target_locs or "anywhere" in target_locs or not loc_lower:
        return True

    if any(tl in loc_lower for tl in target_locs) or "remote" in loc_lower or "india" in loc_lower:
        return True

    return False


def search_workday_portal(page, portal_info, config, ledger, max_jobs=10):
    """
    Search a company Workday career site for target engineering jobs.
    Uses Workday's standard data-automation-id="keywordSearchInput" search bar.
    """
    portal_url = portal_info.get("url")
    company_name = portal_info.get("name", "Company")
    wd_cfg = config.get("workday", {})
    queries = wd_cfg.get("queries", ["Java", "Full Stack", "Software Engineer"])
    applied_keys = set(ledger.get("applied_jobs", {}).keys())

    logging.info(f"Navigating to {company_name} Workday portal: {portal_url}")
    try:
        page.goto(portal_url, wait_until="networkidle", timeout=30000)
    except Exception as e:
        logging.warning(f"Failed to load portal {portal_url}: {e}")
        return []

    human_delay(1.5, 3.0)
    matched_jobs = []

    search_inp = page.locator("input[data-automation-id='keywordSearchInput'], input[type='search'], input[placeholder*='Search' i]").first
    if search_inp.count() == 0:
        logging.warning(f"Could not locate search bar on {company_name} Workday portal.")
        # Try extracting current visible job cards directly
        cards = page.locator("a[data-automation-id='jobTitle']").all()
        for c in cards:
            title = c.inner_text().strip()
            href = c.get_attribute("href") or ""
            full_url = urljoin(portal_url, href)
            if full_url not in applied_keys and is_workday_job_matching(title, "", wd_cfg):
                matched_jobs.append({
                    "company": company_name,
                    "title": title,
                    "url": full_url,
                    "location": "See posting"
                })
        return matched_jobs

    for query in queries[:3]:  # Top 3 queries per portal run
        if len(matched_jobs) >= max_jobs:
            break

        logging.info(f"Searching {company_name} portal for query: '{query}'...")
        try:
            search_inp.click()
            search_inp.fill(query)
            search_inp.press("Enter")
            human_delay(2.5, 4.0)

            cards = page.locator("a[data-automation-id='jobTitle']").all()
            logging.info(f"Found {len(cards)} visible listings for '{query}'.")

            matched_for_q = 0
            for c in cards:
                title = c.inner_text().strip()
                href = c.get_attribute("href") or ""
                full_url = urljoin(portal_url, href)

                if full_url in applied_keys:
                    continue

                # Find associated location snippet if available
                loc_text = ""
                try:
                    card_parent = c.locator("xpath=ancestor::li[1]")
                    if card_parent.count() > 0:
                        loc_el = card_parent.locator("span[data-automation-id*='location' i], div[data-automation-id*='location' i]").first
                        if loc_el.count() > 0:
                            loc_text = loc_el.inner_text().strip()
                except Exception:
                    pass

                if is_workday_job_matching(title, loc_text, wd_cfg):
                    matched_for_q += 1
                    matched_jobs.append({
                        "company": company_name,
                        "title": title,
                        "url": full_url,
                        "location": loc_text or "India / Remote"
                    })
                    if len(matched_jobs) >= max_jobs:
                        break

            # Record search query yield in keyword tracker
            keyword_tracker.record_query_run(
                platform=f"Workday ({company_name})",
                query=query,
                scanned=len(cards),
                matched=matched_for_q,
                applied=0
            )

        except Exception as e:
            logging.debug(f"Search for '{query}' encountered issue: {e}")

    logging.info(f"Collected {len(matched_jobs)} matching role(s) from {company_name}.")
    return matched_jobs


# ---------------------------------------------------------------------------
# Authentication & Candidate Account Handler
# ---------------------------------------------------------------------------

def workday_safe_click(locator, timeout=3000):
    """Click Workday elements safely, bypassing Canvas click_filter overlays."""
    try:
        locator.click(force=True, timeout=timeout)
        return True
    except Exception:
        try:
            locator.evaluate("e => e.click()")
            return True
        except Exception:
            return False


def handle_workday_auth(page, config):
    """
    Handle Workday candidate account creation or sign-in dialog/screen.
    Uses standard password configured in config.json.
    """
    profile = config.get("profile", {})
    wd_cfg = config.get("workday", {})
    email_addr = profile.get("email", "")
    std_pass = wd_cfg.get("standard_password", "CandidatePass123!")

    # Wait up to 6s for page to settle on auth or wizard inputs
    try:
        page.wait_for_selector(
            "input[data-automation-id='email'], button[data-automation-id='bottom-navigation-next-button'], input[type='file'], input[data-automation-id*='legalNameSection_firstName']",
            timeout=6000
        )
    except Exception:
        pass

    # Check if we are on a Sign In / Create Account page
    email_inp = page.locator("input[data-automation-id='email'], input[id*='email' i]").first
    if email_inp.count() == 0 or not email_inp.is_visible(timeout=2500):
        return True  # Already past authentication

    logging.info("Workday candidate authentication screen detected. Processing credentials...")
    human_delay(1.0, 2.0)

    try:
        # Check if this is Create Account form
        verify_pass_inp = page.locator("input[data-automation-id='verifyPassword']").first
        is_create_account = verify_pass_inp.count() > 0 and verify_pass_inp.is_visible(timeout=500)

        pass_inp = page.locator("input[data-automation-id='password']").first

        if is_create_account:
            logging.info(f"Attempting candidate account creation for: {email_addr}")
            email_inp.fill(email_addr)
            pass_inp.fill(std_pass)
            verify_pass_inp.fill(std_pass)

            # Check terms checkbox if present
            terms_cb = page.locator("input[data-automation-id='createAccountCheckbox']").first
            if terms_cb.count() > 0:
                try:
                    terms_cb.check(force=True, timeout=2000)
                except Exception:
                    try:
                        terms_cb.click(force=True, timeout=2000)
                    except Exception:
                        terms_cb.evaluate("e => e.checked = true")

            submit_btn = page.locator("button[data-automation-id='createAccountSubmitButton'], button[type='submit'], div[data-automation-id='click_filter']:has-text('Create Account')").first
            workday_safe_click(submit_btn)
            human_delay(3.5, 5.5)

            # Check if error says account already exists
            body_text = (page.inner_text("body", timeout=2000) or "").lower()
            if "already exists" in body_text or "sign in" in body_text or "duplicate" in body_text:
                logging.info("Account already exists for this tenant. Switching to Sign In...")
                signin_link = page.locator("button[data-automation-id='signInLink'], a:has-text('Sign In')").first
                if signin_link.count() > 0:
                    workday_safe_click(signin_link)
                    human_delay(1.5, 3.0)
                    email_inp = page.locator("input[data-automation-id='email']").first
                    pass_inp = page.locator("input[data-automation-id='password']").first
                    email_inp.fill(email_addr)
                    pass_inp.fill(std_pass)
                    sub_btn = page.locator("button[data-automation-id='signInSubmitButton'], button[type='submit']").first
                    workday_safe_click(sub_btn)
                    human_delay(3.5, 5.5)
        else:
            # Direct Sign In screen
            logging.info(f"Signing in with candidate account: {email_addr}")
            email_inp.fill(email_addr)
            pass_inp.fill(std_pass)
            submit_btn = page.locator("button[data-automation-id='signInSubmitButton'], button[type='submit']").first
            workday_safe_click(submit_btn)
            human_delay(3.5, 5.5)

        return True
    except Exception as e:
        logging.warning(f"Authentication step encountered non-fatal issue: {e}")
        return False


# ---------------------------------------------------------------------------
# Multi-Step Application Wizard Solver
# ---------------------------------------------------------------------------

def answer_workday_screening_field(label_text, config, available_options=None):
    """Determine answer for Workday screening questions."""
    text = (label_text or "").strip().lower()

    # 0. Check universal persistent QA memory first
    saved = qa_memory.recall_answer(label_text, available_options=available_options)
    if saved:
        logging.info(f"[QA Memory] Auto-recalled learned answer for '{label_text[:45]}...' -> '{saved}'")
        return saved

    # 0b. Check AI deterministic semantic classifier
    solv = ai_question_solver.classify_and_solve_deterministic(label_text, options=available_options, config=config)
    if solv:
        logging.info(f"[AI Solver] Deterministically resolved Workday field '{label_text[:45]}...' -> '{solv}'")
        try:
            qa_memory.save_learned_answer(label_text, solv, platform="Workday")
        except Exception:
            pass
        return solv

    qa = config.get("naukri", {}).get("qa_bank", {})
    profile = config.get("profile", {})

    # 0. Account Credentials & Contact
    if any(k in text for k in ("email address", "email", "e-mail")):
        return profile.get("email", "")
    if "password" in text:
        return config.get("workday", {}).get("standard_password", "CandidatePass123!")

    # 1. Authorization
    if any(k in text for k in ("authorized to work in india", "legally authorized to work in india", "right to work")):
        return "Yes"
    if any(k in text for k in ("authorized to work in the u.s", "authorized to work in the united states")):
        return "No"
    if any(k in text for k in ("require sponsorship", "require immigration", "visa sponsorship")):
        if "india" in text:
            return "No"
        return "Yes" if "u.s." in text or "us" in text else "No"

    # 2. Notice period
    if any(k in text for k in ("notice period", "notice", "how soon", "joining time", "start date")):
        days = qa.get("notice_period_days", "7")
        if available_options:
            for opt in available_options:
                if any(k in opt.lower() for k in ("immediate", "7 days", "1 week", "less than 15")):
                    return opt
        return f"{days} days"

    # 3. Salary / CTC
    if any(k in text for k in ("salary", "ctc", "compensation", "expected")):
        is_current = "current" in text or "present" in text
        val = qa.get("current_ctc_lpa", "12") if is_current else qa.get("expected_ctc_lpa", "15")
        return str(val)

    # 4. In-Office / Hybrid / Relocation
    if any(k in text for k in ("relocate", "in-office", "in office", "hybrid", "onsite", "commute")):
        return "Yes"

    # 5. Sanctions / Criminal / Prior Employment
    if any(k in text for k in ("felony", "criminal", "misdemeanor", "convicted", "sanction", "cuba", "iran", "previously employed", "worked for")):
        return "No"

    # 6. Source / How heard
    if any(k in text for k in ("how did you hear", "source", "hear about")):
        if available_options:
            for opt in available_options:
                if any(s in opt.lower() for s in ("linkedin", "company website", "job board")):
                    return opt
        return "LinkedIn"

    # 7. Consent / Terms
    if any(k in text for k in ("consent", "agree", "acknowledge", "terms", "signature")):
        return profile.get("full_name", "Amrit Kumar") if "signature" in text else "Yes"

    # 8. Complex / Behavioral Fallback: Query Local AI (Ollama)
    llm_ans = ai_question_solver.query_ai_for_answer(label_text, options=available_options, config=config)
    if llm_ans:
        try:
            qa_memory.save_learned_answer(label_text, llm_ans, platform="Workday")
        except Exception:
            pass
        return llm_ans

    # Contextual Smart Fallback
    fallback_val = ai_question_solver.generate_contextual_fallback(label_text, options=available_options)

    # 9. Human-in-the-Loop Intervention via Telegram
    tg_cfg = config.get("notifications", {}).get("telegram", {})
    if tg_cfg.get("enabled") and tg_cfg.get("alert_on_unknown_question"):
        user_choice = notifier.notify_stuck(
            platform="Workday ATS",
            company="Enterprise",
            role="Application",
            reason="Unrecognized Screening Question",
            question=label_text,
            options=available_options,
            default_answer=fallback_val
        )
        if user_choice:
            # Cache to qa_bank for automatic recall next time
            qa[label_text] = user_choice
            try:
                qa_memory.save_learned_answer(label_text, user_choice, platform="Workday")
            except Exception:
                pass
            return user_choice

    return fallback_val


def fill_workday_current_step(page, config, resume_pdf_path):
    """
    Inspect the current Workday wizard step and fill contact details,
    upload resume, and solve custom screening questions.
    """
    profile = config.get("profile", {})
    qa = config.get("naukri", {}).get("qa_bank", {})
    wd_cfg = config.get("workday", {})

    full_name = profile.get("full_name", "Amrit Kumar").strip()
    name_parts = full_name.split(maxsplit=1)
    first_name = name_parts[0] if name_parts else "Amrit"
    last_name = name_parts[1] if len(name_parts) > 1 else "Kumar"
    email_addr = profile.get("email", "candidate@example.com")
    phone_num = profile.get("phone", "+91-9876543210").replace("+91-", "").replace("-", "").strip()
    city_val = "Bengaluru"
    postal_val = wd_cfg.get("postal_code", "560001")

    # 0. Check if this is Sign In / Create Account screen
    if page.locator("input[data-automation-id='password'], input[data-automation-id='verifyPassword']").count() > 0:
        handle_workday_auth(page, config)
        return

    # 1. Resume File Upload
    file_input = page.locator("input[type='file'], [data-automation-id*='file-upload-input']").first
    if file_input.count() > 0 and resume_pdf_path and os.path.exists(resume_pdf_path):
        try:
            file_input.set_input_files(resume_pdf_path, timeout=3000)
            logging.info(f"Attached resume PDF -> {os.path.basename(resume_pdf_path)}")
            human_delay(2.0, 3.5)
        except Exception as e:
            logging.debug(f"File upload attempt note: {e}")

    # 2. Contact Information Inputs
    contact_fields = [
        ("input[data-automation-id*='legalNameSection_firstName'], input[data-automation-id='legalNameSection_firstName']", first_name),
        ("input[data-automation-id*='legalNameSection_lastName'], input[data-automation-id='legalNameSection_lastName']", last_name),
        ("input[data-automation-id*='addressSection_addressLine1']", "Bengaluru"),
        ("input[data-automation-id*='addressSection_city']", city_val),
        ("input[data-automation-id*='addressSection_postalCode']", postal_val),
        ("input[data-automation-id*='phone-number'], input[data-automation-id*='phoneNumber']", phone_num),
    ]

    for sel, val in contact_fields:
        try:
            inp = page.locator(sel).first
            if inp.count() > 0 and inp.is_visible(timeout=400):
                curr = (inp.input_value() or "").strip()
                if not curr:
                    inp.fill(val, timeout=1500)
                    logging.info(f"Filled contact field '{sel}' -> {val}")
        except Exception:
            pass

    # 3. Device Type Dropdown (Mobile)
    try:
        phone_device_btn = page.locator("button[data-automation-id*='phone-device-type']").first
        if phone_device_btn.count() > 0 and phone_device_btn.is_visible(timeout=400):
            if "mobile" not in (phone_device_btn.inner_text() or "").lower():
                phone_device_btn.click()
                time.sleep(0.5)
                page.locator("li:has-text('Mobile'), div[role='option']:has-text('Mobile')").first.click(timeout=1500)
    except Exception:
        pass

    # 4. Screening Questions (Radios, Text Inputs, Textareas, Selects)
    field_groups = page.locator("div[data-automation-id*='formField'], fieldset, div[class*='formField']").all()
    for fg in field_groups:
        try:
            # Skip hidden honeypots (e.g. beecatcher)
            if "beecatcher" in (fg.inner_html() or ""):
                continue

            lbl_el = fg.locator("label, legend").first
            lbl_text = (lbl_el.inner_text(timeout=300) or "").strip() if lbl_el.count() > 0 else ""
            if not lbl_text:
                continue

            # A. Radio buttons
            radios = fg.locator("input[type='radio']").all()
            if radios:
                ans = answer_workday_screening_field(lbl_text, config)
                for r in radios:
                    r_lbl = r.locator("..").inner_text().strip().lower()
                    if ans.lower() in r_lbl or (ans.lower() == "yes" and "yes" in r_lbl) or (ans.lower() == "no" and "no" in r_lbl):
                        try:
                            r.check(timeout=800)
                            logging.info(f"Checked radio '{lbl_text[:35]}...' -> {ans}")
                            break
                        except Exception:
                            r.click(timeout=800)
                            break
                continue

            # B. Checkbox
            checkbox = fg.locator("input[type='checkbox']").first
            if checkbox.count() > 0 and checkbox.is_visible(timeout=300):
                if not checkbox.is_checked():
                    try:
                        checkbox.check(timeout=800)
                    except Exception:
                        checkbox.click(timeout=800)
                continue

            # C. Text inputs
            txt_inp = fg.locator("input[type='text'], input[type='number']").first
            if txt_inp.count() > 0 and txt_inp.is_visible(timeout=300):
                curr = (txt_inp.input_value() or "").strip()
                if not curr:
                    ans = answer_workday_screening_field(lbl_text, config)
                    txt_inp.fill(ans, timeout=1500)
                    logging.info(f"Filled question '{lbl_text[:35]}...' -> {ans}")
                continue

        except Exception:
            pass

    # 5. Voluntary Demographics / EEO if present
    eeo_dropdowns = page.locator("button[data-automation-id*='gender'], button[data-automation-id*='veteran'], button[data-automation-id*='disability']").all()
    for eeo_btn in eeo_dropdowns:
        try:
            txt = (eeo_btn.inner_text() or "").lower()
            if not any(k in txt for k in ("male", "no", "decline")):
                eeo_btn.click(timeout=1000)
                time.sleep(0.5)
                opt = page.locator("li:has-text('Decline'), li:has-text('Male'), li:has-text('I am not a protected veteran'), li:has-text('No')").first
                if opt.count() > 0:
                    opt.click(timeout=1000)
        except Exception:
            pass


def solve_workday_application(page, job_info, config, dry_run=False, review_mode=False):
    """
    Execute full multi-step Workday application wizard.
    """
    url = job_info.get("url")
    title = job_info.get("title", "Role")
    company = job_info.get("company", "Enterprise")

    logging.info(f"Opening Workday job posting: {company} — {title}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
    except Exception as e:
        logging.warning(f"Could not load job URL {url}: {e}")
        return False

    human_delay(2.0, 3.5)

    # Extract actual job title and company from Workday job overview
    if title in ("Software Role", "Direct Workday Application", "Role"):
        try:
            t_el = page.locator("h2[data-automation-id='jobPostingHeader'], h1, [data-automation-id='jobPostingTitle']").first
            if t_el.count() > 0:
                title = t_el.inner_text().strip()
        except Exception:
            pass
    if company in ("Tech Company", "Company"):
        try:
            m_co = re.search(r'https?://([^.]+)\.(?:wd\d+\.)?myworkdayjobs\.com', url)
            if m_co:
                company = m_co.group(1).replace('-', ' ').title()
        except Exception:
            pass

    jd_text = ""
    try:
        desc_el = page.locator("div[data-automation-id='jobPostingDescription'], #jobPostingDescription").first
        if desc_el.count() > 0:
            jd_text = desc_el.inner_text()[:4000]
    except Exception:
        pass

    # Generate company-tailored 1-page ATS resume via ATS Bridge
    import ats_bridge
    resume_pdf_path = ats_bridge.resolve_tailored_resume_for_ats(
        config, company=company, job_title=title, jd_text=jd_text
    )

    # Click initial Apply button if on job description overview
    apply_btn = page.locator("div[role='button']:has-text('Apply'), a[data-automation-id='adventureButton'], a[data-automation-id='applyManually'], button:has-text('Apply'), a:has-text('Apply')").first
    if apply_btn.count() > 0 and apply_btn.is_visible(timeout=2000):
        logging.info("Found initial 'Apply' button on job page. Clicking...")
        workday_safe_click(apply_btn)
        human_delay(2.5, 4.0)

    # Click "Autofill with Resume" if option modal appears
    autofill_btn = page.locator("div[role='button']:has-text('Autofill with Resume'), [data-automation-id='autofillWithResume']").first
    if autofill_btn.count() > 0 and autofill_btn.is_visible(timeout=2000):
        logging.info("Found 'Autofill with Resume' option. Advancing...")
        workday_safe_click(autofill_btn)
        human_delay(2.5, 4.0)

    # Handle Candidate Authentication / Account Creation
    handle_workday_auth(page, config)
    human_delay(2.5, 4.0)

    # Multi-step wizard loop
    max_steps = 10
    step = 0
    is_review_ready = False

    while step < max_steps:
        step += 1
        human_delay(1.5, 2.5)

        # 1. Check if we reached the final "Review" page
        review_indicator = page.locator("[data-automation-id='reviewPage'], [data-automation-id='bottom-navigation-submit-button'], h2:has-text('Review'), h3:has-text('Review')").first
        if review_indicator.count() > 0 and review_indicator.is_visible(timeout=1500):
            logging.info(f"[Workday Wizard] Reached final Review step for {company} — {title}!")
            is_review_ready = True
            break

        # 2. Fill current step inputs
        logging.info(f"[Workday Wizard] Filling Step {step}...")
        fill_workday_current_step(page, config, resume_pdf_path)
        human_delay(1.5, 2.5)

        # 3. Check for Next / Save and Continue button
        next_btn = page.locator(
            "div[role='button']:has-text('Save and Continue'), div[role='button']:has-text('Next'), "
            "button[data-automation-id='bottom-navigation-next-button'], button:has-text('Save and Continue'), button:has-text('Next')"
        ).first
        if next_btn.count() > 0 and next_btn.is_visible(timeout=2000):
            logging.info(f"[Workday Wizard] Advancing step {step} via 'Save and Continue'...")
            workday_safe_click(next_btn)
            human_delay(3.0, 5.0)

            # Check if validation error blocked advancement
            err_banner = page.locator("div[data-automation-id='errorMessage'], [aria-invalid='true'], div.alert-danger, [role='alert']:has-text('Error'), [data-automation-id='formErrorAlert']").first
            if err_banner.count() > 0 and err_banner.is_visible(timeout=1000):
                err_text = (err_banner.inner_text() or "Validation error on page").strip()
                logging.warning(f"[Workday Wizard] Form validation blocked progress: {err_text}")
                screenshot_path = os.path.join(logs_dir, f"workday_stuck_{company}_{job_id}.png")
                try:
                    page.screenshot(path=screenshot_path)
                except Exception:
                    pass
                notifier.notify_stuck(
                    platform="Workday ATS",
                    company=company,
                    role=title,
                    reason=f"Step {step} validation error: {err_text}",
                    url=url,
                    screenshot_path=screenshot_path
                )
        else:
            # Check if final submit button is directly present
            sub_btn = page.locator("div[role='button']:has-text('Submit'), button[data-automation-id='bottom-navigation-submit-button'], button:has-text('Submit')").first
            if sub_btn.count() > 0 and sub_btn.is_visible(timeout=1500):
                is_review_ready = True
                break
            logging.info("[Workday Wizard] No further navigation button found. Step complete.")
            break

    # Outcome Handling
    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    job_id = re.search(r'([A-Za-z0-9_-]+)$', url.split('?')[0]).group(1) if re.search(r'([A-Za-z0-9_-]+)$', url.split('?')[0]) else str(int(time.time()))

    # If wizard stopped without reaching review stage
    if not is_review_ready and not dry_run:
        logging.warning(f"[Workday Wizard] Could not advance to review stage for {company} — {title}.")
        screenshot_path = os.path.join(logs_dir, f"workday_stuck_{company}_{job_id}.png")
        try:
            page.screenshot(path=screenshot_path)
        except Exception:
            pass
        notifier.notify_stuck(
            platform="Workday ATS",
            company=company,
            role=title,
            reason=f"Wizard halted before reaching review stage (Step {step})",
            url=url,
            screenshot_path=screenshot_path
        )

    # A. Dry-Run Mode
    if dry_run:
        screenshot_path = os.path.join(logs_dir, f"workday_dry_run_{company}_{job_id}.png")
        try:
            page.screenshot(path=screenshot_path, full_page=True)
            logging.info(f"[DRY-RUN] Form filled successfully! Screenshot saved -> {screenshot_path}")
        except Exception:
            pass
        return True

    # B. Review Mode (Recommended for Workday high-stakes enterprise applications)
    if review_mode or config.get("workday", {}).get("review_mode", False):
        notify_macos(
            "Workday Application Ready!",
            f"All steps filled for {company} ({title}). Please review in Chrome and submit.",
            is_urgent=True
        )
        print(f"\n[Workday Review] Application for {company} — {title} is 100% filled and ready.")
        print("Review the browser window. Press Enter in terminal once submitted, or type 'skip' to abort:")
        user_choice = input(">> ").strip().lower()
        return user_choice != "skip"

    # C. Autonomous Submit Mode
    submit_btn = page.locator("button[data-automation-id='bottom-navigation-submit-button'], button:has-text('Submit')").first
    if submit_btn.count() > 0 and submit_btn.is_visible(timeout=2000):
        logging.info(f"Clicking final Workday Submit button for {company}...")
        submit_btn.click()
        human_delay(4.0, 6.0)

        # Record successful application in keyword tracker
        keyword_tracker.record_query_run(
            platform=f"Workday ({company})",
            query=title,
            scanned=1,
            matched=1,
            applied=1
        )
        return True

    return is_review_ready


# ---------------------------------------------------------------------------
# CLI Command & Batch Runner
# ---------------------------------------------------------------------------

def run_workday_daily_sweep(config, max_applies=None, dry_run=False, force_headed=False, review_mode=False):
    """
    Execute daily Workday application cycle across configured enterprise portals.
    """
    wd_cfg = config.get("workday", {})
    if not wd_cfg.get("enabled", True):
        logging.info("Workday automation is disabled in config.json.")
        return

    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_workday_ledger(logs_dir)

    daily_limit = wd_cfg.get("daily_apply_limit", 20)
    run_limit = max_applies or wd_cfg.get("apply_limit_per_run", 5)

    applies_today = count_applies_today(ledger)
    remaining_daily = max(0, daily_limit - applies_today)
    allowed_this_run = min(run_limit, remaining_daily)

    logging.info(f"Today's Workday Applies: {applies_today}/{daily_limit} (Allowed this run: {allowed_this_run})")
    if allowed_this_run <= 0:
        logging.info("Daily Workday application cap reached. Exiting cycle.")
        return

    portals = wd_cfg.get("target_portals", [])
    if not portals:
        logging.info("No target_portals configured in config.json workday section.")
        return

    with sync_playwright() as playwright:
        browser_context = launch_workday_browser(playwright, config, force_headed=force_headed)
        applied_count = 0

        try:
            for p_info in portals:
                if applied_count >= allowed_this_run:
                    break

                page = browser_context.new_page()
                jobs = search_workday_portal(page, p_info, config, ledger, max_jobs=allowed_this_run - applied_count)
                page.close()

                for job in jobs:
                    if applied_count >= allowed_this_run:
                        break

                    job_url = job.get("url")
                    company = job.get("company")
                    title = job.get("title")

                    logging.info(f"[{applied_count + 1}/{allowed_this_run}] Processing Workday job: {company} — {title}")
                    job_page = browser_context.new_page()
                    success = solve_workday_application(
                        job_page, job, config, dry_run=dry_run, review_mode=review_mode
                    )
                    job_page.close()

                    if success:
                        applied_count += 1
                        if not dry_run:
                            ledger["applied_jobs"][job_url] = {
                                "company": company,
                                "title": title,
                                "location": job.get("location"),
                                "url": job_url,
                                "status": "APPLIED",
                                "applied_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                            }
                            ledger["last_run_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                            save_workday_ledger(logs_dir, ledger)

                            notify_macos(
                                "Workday Application Complete!",
                                f"Applied to {company} for {title}.",
                                is_urgent=True
                            )

                    human_delay(3.0, 6.0)

        finally:
            browser_context.close()

    logging.info(f"Completed Workday cycle. Processed {applied_count} application(s).")


def apply_single_url(url, config, dry_run=False, force_headed=False, review_mode=False):
    """
    Directly solve and apply to a specific Workday job link provided via CLI or Dropzone.
    """
    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_workday_ledger(logs_dir)

    # Infer company from subdomain: e.g. https://adobe.wd5.myworkdayjobs.com -> Adobe
    netloc = urlparse(url).netloc
    company = netloc.split('.')[0].capitalize() if netloc else "Company"

    job_info = {
        "url": url,
        "title": "Direct Workday Application",
        "company": company,
    }

    with sync_playwright() as playwright:
        browser_context = launch_workday_browser(playwright, config, force_headed=force_headed)
        try:
            page = browser_context.new_page()
            success = solve_workday_application(
                page, job_info, config, dry_run=dry_run, review_mode=review_mode
            )
            page.close()
            if success and not dry_run:
                ledger["applied_jobs"][url] = {
                    "company": company,
                    "title": job_info["title"],
                    "url": url,
                    "status": "APPLIED",
                    "applied_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                }
                save_workday_ledger(logs_dir, ledger)
                logging.info(f"Successfully recorded Workday application for {url}")
        finally:
            browser_context.close()


def print_status(config):
    """Print current ledger and daily metrics for Workday applications."""
    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_workday_ledger(logs_dir)
    today_count = count_applies_today(ledger)
    total_count = len(ledger.get("applied_jobs", {}))
    daily_limit = config.get("workday", {}).get("daily_apply_limit", 20)

    print("\n" + "=" * 55)
    print("         WORKDAY AUTOMATION ENGINE STATUS")
    print("=" * 55)
    print(f" • Last Run:           {ledger.get('last_run_at') or 'Never'}")
    print(f" • Applies Today:      {today_count} / {daily_limit} (Daily Limit)")
    print(f" • Total Tracked:      {total_count} Applications")
    print(f" • Target Portals:     {len(config.get('workday', {}).get('target_portals', []))} Portals Configured")
    print("=" * 55 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Workday Automated Job Discovery & Multi-Step Wizard Solver")
    parser.add_argument("--url", type=str, help="Apply directly to a single Workday job URL")
    parser.add_argument("--portal", type=str, help="Target a specific portal by name (e.g. Autodesk, Adobe, Nvidia)")
    parser.add_argument("--limit", type=int, help="Override maximum applications for this run")
    parser.add_argument("--dry-run", action="store_true", help="Fill wizard and save screenshot without submitting")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser window")
    parser.add_argument("--review", action="store_true", help="Fill form and pause at Review screen for manual confirmation")
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
        elif args.portal:
            # Filter target portals
            filtered = [
                p for p in config.get("workday", {}).get("target_portals", [])
                if args.portal.lower() in p.get("name", "").lower()
            ]
            if filtered:
                config["workday"]["target_portals"] = filtered
                run_workday_daily_sweep(
                    config,
                    max_applies=args.limit,
                    dry_run=args.dry_run,
                    force_headed=args.headed,
                    review_mode=args.review
                )
            else:
                logging.error(f"No configured portal matching name '{args.portal}'.")
        else:
            run_workday_daily_sweep(
                config,
                max_applies=args.limit,
                dry_run=args.dry_run,
                force_headed=args.headed,
                review_mode=args.review
            )


if __name__ == "__main__":
    main()
