#!/usr/bin/env python3
"""
Foundit.in (formerly Monster India) Automation Engine
======================================================
Automates:
  1. Persistent Headed/Headless Google Chrome browser context for Foundit (browser_profile/foundit).
  2. One-time interactive authentication session saving to browser_profile/foundit.
  3. Daily candidate profile/CV refresh with freshly dated Master Resume PDF.
  4. Targeted job search & evaluation matching Java 21, Spring Boot 3, Microservices, React 19, FastAPI, AWS.
  5. 1-Click Quick Apply & Screening Questionnaire Solver (CTC, Notice Period, Experience).
  6. Dropzone HR Email Bridge: Extracts recruiter & HR emails from external postings into Dropzone
     for instant automated cold outreach via agent.py.
  7. Strict daily anti-bot safety caps & persistent ledger tracking in logs/foundit_applied_jobs.json.
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
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import agent as email_agent
import naukri_agent
import power_utils
from power_utils import MacKeepAwake

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Foundit] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.expanduser("~/Documents/JobAutomation/logs/automation.log"),
            mode="a",
            encoding="utf-8"
        )
    ]
)


def human_delay(min_sec=1.5, max_sec=3.5):
    """Introduce randomized delay mimicking human interaction."""
    time.sleep(random.uniform(min_sec, max_sec))


def notify_macos(title, message, is_urgent=False):
    """Display a non-blocking macOS notification banner respecting quiet_mode."""
    power_utils.notify_macos(title, message, is_urgent=is_urgent)


def get_foundit_ledger_path(logs_dir):
    return os.path.join(logs_dir, 'foundit_applied_jobs.json')


def load_foundit_ledger(logs_dir):
    """Load persistent ledger of applied Foundit jobs and profile refresh state."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = get_foundit_ledger_path(logs_dir)
    default_data = {
        "login_verified": False,
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
            logging.warning(f"[Foundit] Could not read foundit_applied_jobs.json: {e}")
    return default_data


def save_foundit_ledger(logs_dir, ledger):
    """Persist Foundit applied jobs and profile refresh state safely."""
    ledger_path = get_foundit_ledger_path(logs_dir)
    tmp_path = ledger_path + ".tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp_path, ledger_path)
    except Exception as e:
        logging.warning(f"[Foundit] Could not save foundit_applied_jobs.json: {e}")


def count_applies_today(ledger):
    """Count how many Foundit jobs were successfully applied to today (YYYY-MM-DD)."""
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


def launch_foundit_browser(playwright, config, force_headed=False):
    """Launch a persistent Google Chrome context dedicated to Foundit automation."""
    fnd_cfg = config.get('foundit', {})
    profile_dir = os.path.expanduser(
        fnd_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/foundit')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = fnd_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(fnd_cfg.get('headless', False))
    if force_headed:
        headless = False

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
    if not force_headed and (os.environ.get("JOB_AUTO_OFFSCREEN") == "1" or fnd_cfg.get("offscreen", False)):
        launch_kwargs["args"].append("--window-position=-3000,-3000")

    if os.path.exists(chrome_exec):
        launch_kwargs["executable_path"] = chrome_exec

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.addEventListener('DOMContentLoaded', () => {
            const googlePrompt = document.getElementById('credential_picker_container');
            if (googlePrompt) googlePrompt.remove();
        });
    """)
    return context


def safe_foundit_navigate(page, url, fallback_url=None, timeout=35000):
    """
    Safely navigate to a Foundit URL with automatic redirect-loop and HTTPS protection.
    If ERR_TOO_MANY_REDIRECTS or Access Denied occurs, retries with clean HTTPS URL.
    """
    if url.startswith("http://"):
        url = "https://" + url[7:]
    if fallback_url and fallback_url.startswith("http://"):
        fallback_url = "https://" + fallback_url[7:]

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        human_delay(1.5, 2.5)
        # Check if the page landed on an error or redirect loop page
        body_text = ""
        try:
            body_text = (page.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception:
            pass
        if "access denied" in body_text and "edgesuite.net" in body_text:
            logging.warning(f"[Foundit] Akamai Access Denied on {url}. Navigating to dashboard via HTTPS...")
            page.goto("https://www.foundit.in/seeker/dashboard", wait_until="domcontentloaded", timeout=timeout)
            human_delay(1.5, 2.5)
            return True
        if "redirected you too many times" in body_text or "err_too_many_redirects" in body_text:
            raise Exception("Detected ERR_TOO_MANY_REDIRECTS in page body")
        return True
    except Exception as e:
        err_msg = str(e).lower()
        if "redirect" in err_msg or "err_too_many_redirects" in err_msg:
            logging.warning(f"[Foundit] Redirect loop detected for {url}. Retrying with dashboard...")
            target = fallback_url or "https://www.foundit.in/seeker/dashboard"
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=timeout)
                human_delay(1.5, 2.5)
                return True
            except Exception as e2:
                logging.warning(f"[Foundit] Fallback navigation failed: {e2}")
                try:
                    page.goto("https://www.foundit.in/seeker-profile/login", wait_until="domcontentloaded", timeout=timeout)
                    return True
                except Exception:
                    return False
        else:
            logging.warning(f"[Foundit] Navigation warning for {url}: {e}")
            return False


def is_logged_in_to_foundit(page):
    """Check whether the current Foundit page represents an authenticated seeker session."""
    url = (page.url or "").lower()

    # 1. Obvious unauthenticated URLs
    if any(k in url for k in ("login", "registration", "signup", "srp/auth")):
        return False

    # 2. Check for login form or input fields that only appear when logged out
    login_indicators = [
        "input[type='password']",
        "input[placeholder*='Password' i]",
        "input[placeholder*='Email' i]",
        "input[placeholder*='Mobile' i]",
        "button:has-text('Sign In')",
        "button:has-text('Log in')",
        "button:has-text('Send OTP')",
        "button:has-text('Login with OTP')",
        "a:has-text('Register for free')",
        "form[class*='login' i]",
        "div[class*='loginContainer' i]",
        "div[class*='loginModal' i]"
    ]
    for sel in login_indicators:
        try:
            if page.locator(sel).first.is_visible(timeout=300):
                return False
        except Exception:
            pass

    # 3. Direct URL match for authenticated seeker dashboard or candidate profile
    if "foundit.in" in url and any(k in url for k in ("seeker/dashboard", "seeker-profile/candidate", "seeker-profile/profile")):
        return True

    # 4. Check for elements that ONLY exist when genuinely authenticated
    authenticated_selectors = [
        "span:has-text('Hi, ')",
        "div:has-text('Hi, ')",
        "span:has-text('Recommended jobs')",
        "button:has-text('Recommended jobs')",
        "span:has-text('Applied jobs')",
        "button:has-text('Applied jobs')",
        "span:has-text('Saved jobs')",
        "button:has-text('Saved jobs')",
        "a[href*='logout' i]",
        "button:has-text('Logout')",
        "span:has-text('Logout')",
        "a:has-text('Logout')",
        "div[class*='userProfileCard']",
        "div[class*='user-avatar']",
        "div.profile_pic",
        "div[class*='header-profile']",
        "a[href*='/seeker-profile/candidate']",
        "div[class*='seekerDashboard']",
        "div[class*='dashboardContainer']"
    ]
    for sel in authenticated_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            pass

    # 5. Check cookies for confirmed authentication token (MSSOAT is Monster/Foundit SSO token)
    try:
        cookies = page.context.cookies()
        auth_cookie_names = ("MSSOAT", "MSSOCLIENT", "MSAL", "seeker_token", "auth_token", "monster_token", "user_session", "session_token")
        for c in cookies:
            if c.get("name") in auth_cookie_names and c.get("value"):
                if "foundit.in" in url and not any(k in url for k in ("login", "registration", "signup", "srp/auth")):
                    return True
    except Exception:
        pass

    return False


def ensure_logged_in(page, interactive_wait_seconds=300):
    """
    Navigate to Foundit and verify login.
    If not logged in, wait for the user to log in once in the opened Chrome window.
    """
    logging.info("[Foundit] Navigating to Foundit to verify session...")

    # First check if current session is already authenticated
    try:
        for p in list(page.context.pages):
            if is_logged_in_to_foundit(p):
                logging.info("[Foundit] Active Foundit session confirmed!")
                return True
    except Exception:
        pass

    print("\n" + "=" * 65)
    print("  [ACTION REQUIRED] PLEASE LOG IN TO FOUNDIT.IN IN CHROME")
    print("  Google Chrome has opened with the Foundit candidate login page.")
    print("  Please log in using your Foundit account credentials (or Google SSO).")
    print(f"  Waiting up to {interactive_wait_seconds} seconds for login to complete...")
    print("=" * 65 + "\n")
    notify_macos("Foundit Automation", "Please log in once in the opened Chrome window.", is_urgent=True)

    # Clean direct navigation to the official seeker login URL
    success = safe_foundit_navigate(page, "https://www.foundit.in/seeker-profile/login", fallback_url="https://www.foundit.in/")
    human_delay(2.0, 3.5)

    if not success or "login" not in (page.url or "").lower():
        # Fallback to homepage and click login button
        try:
            page.goto("https://www.foundit.in/", wait_until="domcontentloaded", timeout=25000)
            human_delay(1.5, 2.5)
            login_btn = page.locator("a:has-text('Login'), button:has-text('Login'), a:has-text('Sign In'), span:has-text('Login')").first
            if login_btn.count() > 0 and login_btn.is_visible(timeout=2000):
                login_btn.click(timeout=3000)
                human_delay(1.5, 2.5)
        except Exception:
            pass

    try:
        page.bring_to_front()
    except Exception:
        pass

    start_time = time.time()
    while time.time() - start_time < interactive_wait_seconds:
        time.sleep(2)
        try:
            for p in list(page.context.pages):
                if is_logged_in_to_foundit(p):
                    logging.info("[Foundit] Login detected! Saving persistent session.")
                    print("[SUCCESS] Foundit login detected and saved to persistent Chrome profile!")
                    human_delay(2.0, 3.5)
                    return True
        except Exception:
            pass

    logging.error("[Foundit] Timed out waiting for Foundit login.")
    return False


def refresh_foundit_profile(page, config, ledger, logs_dir):
    """
    Daily Profile Refresher on Foundit:
      1. Generates freshly dated Master Resume PDF.
      2. Uploads via profile resume uploader to reset candidate freshness timestamp.
    """
    logging.info("[Foundit] Starting daily profile refresh...")
    try:
        # Navigate to modern candidate profile
        profile_url = "https://www.foundit.in/seeker-profile/candidate"
        safe_foundit_navigate(page, profile_url, fallback_url="https://www.foundit.in/seeker/profile")
        human_delay(3.0, 5.0)

        # Generate fresh master resume PDF
        fresh_pdf = naukri_agent.ensure_master_resume_pdf(config)
        refreshed = False

        # Attempt 1: Look directly for input[type='file'] in the DOM
        file_inputs = page.locator("input[type='file']")
        if file_inputs.count() > 0:
            try:
                file_inputs.first.set_input_files(fresh_pdf)
                human_delay(2.5, 4.5)
                logging.info("[Foundit] Uploaded fresh Master Resume PDF via direct file input!")
                refreshed = True
            except Exception as e:
                logging.warning(f"[Foundit] Direct file input failed: {e}")

        # Attempt 2: If not refreshed yet, find the Update/Upload button and handle file chooser
        if not refreshed:
            update_selectors = [
                "button:has-text('Update Resume')",
                "button:has-text('Upload Resume')",
                "button:has-text('Replace Resume')",
                "a:has-text('Update Resume')",
                "a:has-text('Upload Resume')",
                "span:has-text('Update Resume')",
                "span:has-text('Upload Resume')",
                "div:has-text('Update Resume')",
                "div:has-text('Upload Resume')",
                "div[class*='resume'] button",
                "div[class*='resume'] a"
            ]
            for sel in update_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=1000):
                        try:
                            with page.expect_file_chooser(timeout=3500) as fc_info:
                                btn.click(timeout=2000)
                            file_chooser = fc_info.value
                            file_chooser.set_files(fresh_pdf)
                            human_delay(2.5, 4.5)
                            logging.info(f"[Foundit] Uploaded resume via file chooser triggered by '{sel}'!")
                            refreshed = True
                            break
                        except Exception:
                            # Might have just revealed the file input instead of native dialog
                            human_delay(1.0, 2.0)
                            file_inputs = page.locator("input[type='file']")
                            if file_inputs.count() > 0:
                                file_inputs.first.set_input_files(fresh_pdf)
                                human_delay(2.5, 4.5)
                                logging.info("[Foundit] Uploaded fresh resume via revealed file input!")
                                refreshed = True
                                break
                except Exception:
                    continue

        # Click save / submit / update confirmation if present
        save_btn = page.locator(
            "button:has-text('Save'), button:has-text('Update'), button:has-text('Submit'), "
            "button.btn-primary, button[type='submit']"
        ).first
        if save_btn.count() > 0 and save_btn.is_visible(timeout=1500):
            save_btn.click(timeout=3000)
            human_delay(2.0, 3.5)
            refreshed = True

        ledger["last_profile_refresh_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if refreshed:
            ledger["profile_refresh_count"] = int(ledger.get("profile_refresh_count", 0)) + 1
            save_foundit_ledger(logs_dir, ledger)
            logging.info("[Foundit] Daily Profile Refresh completed successfully!")
            print("[Foundit] Daily Profile Refresh completed successfully!")
            return True
        else:
            save_foundit_ledger(logs_dir, ledger)
            logging.info("[Foundit] Profile visited and active timestamp updated.")
            print("[Foundit] Profile visited and activity timestamp refreshed.")
            return True
    except Exception as e:
        logging.warning(f"[Foundit] Profile refresh error: {e}")
        return False


def is_job_suitable(title, snippet, config):
    """Check if the role matches Amrit's tech stack and does not contain excluded keywords."""
    fnd_cfg = config.get("foundit", {})
    search_cfg = fnd_cfg.get("search", {})
    excluded = search_cfg.get("excluded_title_keywords", [
        ".net", "c#", "php", "wordpress", "salesforce", "sap ", "abap", "manual testing", "qa automation", "sdet", "director", "vp of", "lead architect"
    ])
    combined_text = f"{title} {snippet}".lower()
    for ex in excluded:
        if ex.lower() in combined_text:
            return False, f"Contains excluded keyword '{ex}'"

    # Check for excessive experience requirement (> 5 years)
    exp_matches = re.findall(r'(\d+)\+?\s*(?:to|-)\s*(\d+)?\s*(?:years|yrs|yo?e)', combined_text)
    for m in exp_matches:
        try:
            min_exp = int(m[0])
            if min_exp > search_cfg.get("max_min_experience_allowed", 5):
                return False, f"Requires {min_exp}+ YOE (exceeds target range)"
        except Exception:
            pass

    # Match positive tech keywords
    tech_keywords = [
        "java", "spring", "microservices", "react", "fastapi", "python",
        "backend", "full stack", "fullstack", "software engineer", "developer"
    ]
    has_tech = any(tk in combined_text for tk in tech_keywords)
    if not has_tech:
        return False, "Does not match target tech stack (Java/Spring/React/FastAPI/Backend)"

    return True, "Role is suitable"


def bridge_hr_emails_to_dropzone(jd_text, title, company, job_id, config):
    """Extract HR emails from JD text and create a ticket in Dropzone for agent.py."""
    fnd_cfg = config.get("foundit", {})
    if not fnd_cfg.get("search", {}).get("extract_hr_emails_to_dropzone", True):
        return []

    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', jd_text or "")
    ignore_domains = ['foundit.in', 'monsterindia.com', 'monster.com', 'example.com', 'w3.org', 'schema.org', 'sentry.io']
    valid_emails = [
        e for e in emails
        if not any(x in e.lower() for x in ("support@", "info@", "help@", "donotreply@", "no-reply@"))
        and not any(dom in e.lower() for dom in ignore_domains)
    ]
    if not valid_emails:
        return []

    dropzone_dir = email_agent.get_dropzone_path(config)
    os.makedirs(dropzone_dir, exist_ok=True)
    clean_company = re.sub(r'[^a-zA-Z0-9_]', '_', company.strip())
    filename = f"foundit_{clean_company}_{job_id}.txt"
    file_path = os.path.join(dropzone_dir, filename)

    content = f"""SOURCE: Foundit.in (Monster India)
COMPANY: {company}
ROLE: {title}
JOB_ID: {job_id}
CONTACT_EMAILS: {', '.join(valid_emails)}

JOB DESCRIPTION:
{jd_text}
"""
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logging.info(f"[Foundit -> Email Bridge] Dropped HR email ticket: {filename} ({valid_emails})")
    except Exception as e:
        logging.warning(f"[Foundit -> Email Bridge] Failed to write dropzone file: {e}")

    return valid_emails


def solve_foundit_application_modal(page, config):
    """
    Handle any multi-step modal or confirmation dialog triggered after clicking Apply on Foundit.
    Answers screening questions using the centralized QA bank.
    """
    qa = config.get("naukri", {}).get("qa_bank", {})
    profile = config.get("profile", {})
    max_steps = 6
    step = 0

    while step < max_steps:
        step += 1
        human_delay(1.5, 2.5)

        modal = page.locator(
            "div[role='dialog'], div.modal, div[class*='modal'], div[class*='applyDialog'], "
            "div[class*='apply-modal'], div[class*='applicationContainer']"
        ).first
        if modal.count() == 0 or not modal.is_visible(timeout=1200):
            # No modal or modal closed, application may have completed directly
            return True

        # Check for final success indicator
        success_indicator = modal.locator(
            "h1:has-text('Applied'), h2:has-text('Applied'), div:has-text('Application submitted'), "
            "span:has-text('Successfully Applied'), p:has-text('Application sent')"
        ).first
        if success_indicator.count() > 0 and success_indicator.is_visible(timeout=1000):
            logging.info("[Foundit Modal] Application submission confirmed!")
            close_btn = modal.locator("button:has-text('Close'), button[aria-label='Close'], i.fa-close, button.close").first
            if close_btn.count() > 0 and close_btn.is_visible(timeout=1000):
                close_btn.click(timeout=1500)
            return True

        # Check for final submission / confirm button
        submit_btn = modal.locator(
            "button:has-text('Submit'), button:has-text('Apply Now'), button:has-text('Confirm'), "
            "button:has-text('Submit Application'), button.btn-primary"
        ).first

        # Handle screening inputs inside modal
        inputs = modal.locator("input[type='text'], input[type='number'], input[type='tel']").all()
        for inp in inputs:
            try:
                if not inp.is_visible(timeout=300):
                    continue
                val = (inp.input_value() or "").strip()
                if val:
                    continue

                parent_text = (inp.evaluate("el => el.closest('div, label, fieldset')?.innerText") or "").lower()
                ans = "3"
                if any(k in parent_text for k in ("notice", "days", "joining")):
                    ans = qa.get("notice_period_days", "7")
                elif any(k in parent_text for k in ("current ctc", "current salary", "present ctc")):
                    ans = qa.get("current_ctc_lpa", "12")
                elif any(k in parent_text for k in ("expected ctc", "expected salary")):
                    ans = qa.get("expected_ctc_lpa", "15")
                elif any(k in parent_text for k in ("phone", "mobile", "contact")):
                    ans = profile.get("phone", "+91-9876543210")
                elif any(k in parent_text for k in ("experience", "years")):
                    ans = qa.get("relevant_experience_years", "3")

                inp.fill(ans, timeout=1200)
            except Exception:
                pass

        # Handle Radio Buttons (Yes / No)
        radio_yes = modal.locator("input[type='radio'][value*='yes' i], label:has-text('Yes')").first
        if radio_yes.count() > 0 and radio_yes.is_visible(timeout=400):
            try:
                radio_yes.click(timeout=1000)
            except Exception:
                pass

        # Click submit or continue button
        if submit_btn.count() > 0 and submit_btn.is_visible(timeout=1500):
            logging.info("[Foundit Modal] Clicking submit/confirm button...")
            submit_btn.click(timeout=3000)
            human_delay(2.0, 3.5)
            return True

        continue_btn = modal.locator(
            "button:has-text('Next'), button:has-text('Continue'), button:has-text('Proceed')"
        ).first
        if continue_btn.count() > 0 and continue_btn.is_visible(timeout=1500):
            continue_btn.click(timeout=3000)
            human_delay(1.5, 2.5)
        else:
            break

    return True


def apply_to_foundit_jobs(page, config, ledger, logs_dir, max_applies=None):
    """
    Search Foundit across target queries and execute 1-Click / Quick Apply.
    """
    fnd_cfg = config.get("foundit", {})
    search_cfg = fnd_cfg.get("search", {})
    queries = search_cfg.get("queries", [
        "Java Full Stack Developer",
        "Spring Boot Microservices Developer",
        "Java Backend Developer",
        "Full Stack Developer React Java",
        "Python FastAPI Developer"
    ])
    locations = search_cfg.get("locations", ["Bengaluru", "Hyderabad", "Pune", "Remote", "India"])
    daily_limit = fnd_cfg.get("daily_apply_limit", 50)
    per_run_limit = fnd_cfg.get("apply_limit_per_run", 25)

    applied_today = count_applies_today(ledger)
    remaining_daily = max(0, daily_limit - applied_today)
    if remaining_daily <= 0:
        logging.info(f"[Foundit] Daily apply limit reached ({applied_today}/{daily_limit}).")
        print(f"[Foundit] Daily apply limit reached ({applied_today}/{daily_limit}).")
        return 0

    target_applies = min(remaining_daily, per_run_limit)
    if max_applies is not None:
        target_applies = min(target_applies, int(max_applies))

    logging.info(f"[Foundit] Target applies for this run: {target_applies} (Today so far: {applied_today}/{daily_limit})")

    applied_count = 0
    applied_records = ledger.get("applied_jobs", {})

    for query in queries:
        if applied_count >= target_applies:
            break

        for loc in locations[:2]:  # Prioritize top 2 locations per query to maintain high relevance
            if applied_count >= target_applies:
                break

            encoded_q = quote_plus(query.strip())
            encoded_loc = quote_plus(loc.strip())
            search_url = f"https://www.foundit.in/srp/results?query={encoded_q}&locations={encoded_loc}&experience=3"

            logging.info(f"[Foundit] Searching: '{query}' in '{loc}' -> {search_url}")
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                human_delay(3.0, 5.0)
            except Exception as e:
                logging.warning(f"[Foundit] Search navigation warning: {e}")
                continue

            # Check for job cards (individual tuples)
            cards = page.locator("div.srpResultCardContainer, div.cardContainer")
            card_count = cards.count()
            logging.info(f"[Foundit] Found {card_count} job cards for '{query}' in '{loc}'")

            for idx in range(card_count):
                if applied_count >= target_applies:
                    break

                try:
                    card = cards.nth(idx)
                    card.scroll_into_view_if_needed(timeout=2000)
                    card_text = (card.inner_text(timeout=2000) or "")

                    # Extract title and company
                    title_elem = card.locator("h2, h3, a[class*='title'], [class*='jobTitle'], a[class*='job-title']").first
                    title = (title_elem.inner_text(timeout=1000) or "").strip() if title_elem.count() > 0 else "Software Engineer"
                    company_elem = card.locator("[class*='companyName'], [class*='company'], div.company-name").first
                    company = (company_elem.inner_text(timeout=1000) or "").strip() if company_elem.count() > 0 else "Tech Company"

                    job_id = f"fnd_{abs(hash(company + title)) % 10000000}"
                    if job_id in applied_records and applied_records[job_id].get("status") == "APPLIED":
                        continue

                    suitable, reason = is_job_suitable(title, card_text, config)
                    if not suitable:
                        continue

                    # Bridge any HR email in text to Dropzone
                    bridge_hr_emails_to_dropzone(card_text, title, company, job_id, config)

                    # Click into card or title to open JD in right pane
                    logging.info(f"[Foundit] Selecting job card for '{title}' at '{company}'...")
                    try:
                        clickable = title_elem if (title_elem.count() > 0 and title_elem.is_visible(timeout=1000)) else card
                        clickable.click(timeout=3000)
                    except Exception:
                        card.click(timeout=3000)
                    human_delay(1.5, 2.5)

                    # Extract deeper JD text from right pane
                    detail_loc = page.locator("div.srpJdContainer, div.jdContent, div.srpJdSection, div[class*='jobDescription']").first
                    if detail_loc.count() > 0:
                        try:
                            deep_jd = detail_loc.inner_text(timeout=2000) or ""
                            if deep_jd:
                                bridge_hr_emails_to_dropzone(deep_jd, title, company, job_id, config)
                        except Exception:
                            pass

                    # Locate Apply button in right pane
                    detail_apply_btn = page.locator(
                        "div.srpJdContainer button:has-text('Apply'), div.applyBtnCont button, div.applyBtnCont, div.btnFunctionality button"
                    ).first

                    if detail_apply_btn.count() == 0 or not detail_apply_btn.is_visible():
                        logging.info(f"[Foundit] No visible Apply CTA found in detail view for '{title}'. Skipping.")
                        continue

                    btn_text = (detail_apply_btn.inner_text() or "").strip().lower()
                    if "applied" in btn_text:
                        applied_records[job_id] = {"company": company, "title": title, "status": "ALREADY_APPLIED"}
                        continue

                    logging.info(f"[Foundit] Clicking Apply for '{title}' at '{company}'...")
                    detail_apply_btn.click(timeout=3000)
                    human_delay(2.0, 3.5)
                    submitted = solve_foundit_application_modal(page, config)

                    # Close any modal / drawer left open on main page
                    try:
                        close_btn = page.locator("button[aria-label='Close'], i.fa-close, button.close, div.closeBtn").first
                        if close_btn.count() > 0 and close_btn.is_visible(timeout=1000):
                            close_btn.click(timeout=1500)
                    except Exception:
                        pass

                    if submitted:
                        applied_records[job_id] = {
                            "company": company,
                            "title": title,
                            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "status": "APPLIED",
                            "channel": "Foundit"
                        }
                        applied_count += 1
                        save_foundit_ledger(logs_dir, ledger)
                        logging.info(f"[Foundit] Successfully applied to '{title}' at '{company}' ({applied_count}/{target_applies})")
                        print(f"[Foundit] Successfully applied: '{title}' at '{company}'")

                    human_delay(2.5, 4.5)
                except Exception as e:
                    logging.warning(f"[Foundit] Error processing card #{idx}: {e}")
                    continue

    save_foundit_ledger(logs_dir, ledger)
    notify_macos("Foundit Auto-Applier", f"Finished run: Applied to {applied_count} jobs today.")
    return applied_count


def run_foundit_pipeline(mode="apply", max_applies=None, force_headed=False):
    """Entry point for Foundit automation pipeline."""
    config = email_agent.load_config()
    fnd_cfg = config.get("foundit", {})
    if not fnd_cfg.get("enabled", True):
        logging.info("[Foundit] Foundit agent is disabled in config.json.")
        return

    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_foundit_ledger(logs_dir)

    needs_initial_login = not ledger.get("login_verified", False)
    is_headed = force_headed or (mode == "login") or needs_initial_login

    try:
        with sync_playwright() as playwright:
            context = launch_foundit_browser(playwright, config, force_headed=is_headed)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.bring_to_front()
            except Exception:
                pass

            try:
                wait_secs = 300 if (mode == "login" or needs_initial_login) else 30
                logged_in = ensure_logged_in(page, interactive_wait_seconds=wait_secs)
                if not logged_in:
                    logging.error("[Foundit] Authentication required. Run with --login-foundit to authenticate.")
                    print("[Foundit] Authentication required. Please run: python3 run_all.py --login-foundit")
                    return

                ledger["login_verified"] = True
                save_foundit_ledger(logs_dir, ledger)

                if mode == "login":
                    logging.info("[Foundit] Login verification complete! Persistent profile is ready.")
                    print("\n" + "=" * 65)
                    print("  [SUCCESS] Foundit session successfully verified and saved!")
                    print("  Persistent Chrome profile: browser_profile/foundit")
                    print("  Closing browser window in 3 seconds...")
                    print("=" * 65 + "\n")
                    notify_macos("Foundit Automation", "Login verified successfully! Persistent profile saved.")
                    time.sleep(3)
                    return

                if mode in ("refresh", "all"):
                    refresh_foundit_profile(page, config, ledger, logs_dir)

                if mode in ("apply", "all"):
                    apply_to_foundit_jobs(page, config, ledger, logs_dir, max_applies=max_applies)

            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[Foundit] Execution cancelled by user.")
        return


def main():
    parser = argparse.ArgumentParser(description="Foundit Automation Agent")
    parser.add_argument("--mode", choices=["login", "refresh", "apply", "all"], default="apply", help="Operating mode.")
    parser.add_argument("--max-applies", type=int, default=None, help="Maximum jobs to apply in this session.")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser window.")
    parser.add_argument("--headless", action="store_true", help="Force headless browser execution.")
    args = parser.parse_args()

    force_headed = False
    if args.headed or args.mode == "login":
        force_headed = True
    elif args.headless:
        force_headed = False

    with MacKeepAwake():
        run_foundit_pipeline(mode=args.mode, max_applies=args.max_applies, force_headed=force_headed)


if __name__ == "__main__":
    main()
