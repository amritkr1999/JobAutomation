#!/usr/bin/env python3
"""
Jobgether.com Automation Engine (Remote & Global Tech Jobs)
===========================================================
Automates:
  1. Persistent Headed/Headless Google Chrome browser context for Jobgether.
  2. One-time interactive authentication session saving to browser_profile/jobgether.
  3. Daily candidate profile/CV refresh with freshly dated Master Resume PDF.
  4. Remote job search & evaluation matching Java 21, Spring Boot 3, Microservices, React 19, FastAPI, AWS.
  5. 1-Click apply & application submission.
  6. Dropzone HR Email Bridge: Extracts recruiter & HR emails from external postings into Dropzone
     for instant automated cold outreach via agent.py.
  7. Strict daily anti-bot safety caps & persistent ledger tracking in logs/jobgether_applied_jobs.json.
"""

import os
import re
import json
import time
import random
import logging
import argparse
from datetime import datetime
from playwright.sync_api import sync_playwright

import agent as email_agent
import naukri_agent
from power_utils import MacKeepAwake

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Jobgether] %(message)s",
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
    """Deliver desktop notifications on macOS."""
    naukri_agent.notify_macos(title, message, is_urgent=is_urgent)


def get_jobgether_ledger_path(logs_dir):
    return os.path.join(logs_dir, "jobgether_applied_jobs.json")


def load_jobgether_ledger(logs_dir):
    """Load persistent ledger of applied jobs and refresh status."""
    path = get_jobgether_ledger_path(logs_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Could not parse jobgether ledger, initializing fresh: {e}")
    return {
        "applied_jobs": {},
        "last_profile_refresh_at": None,
        "profile_refresh_count": 0,
        "login_verified": False
    }


def save_jobgether_ledger(logs_dir, ledger):
    """Persist ledger to disk safely."""
    path = get_jobgether_ledger_path(logs_dir)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def count_applies_today(ledger):
    """Count how many Jobgether jobs were successfully applied to today (YYYY-MM-DD)."""
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


def launch_jobgether_browser(playwright, config, force_headed=False):
    """Launch a persistent Google Chrome context dedicated to Jobgether automation."""
    jg_cfg = config.get('jobgether', {})
    profile_dir = os.path.expanduser(
        jg_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/jobgether')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = jg_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(jg_cfg.get('headless', False))
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
    if not force_headed and (os.environ.get("JOB_AUTO_OFFSCREEN") == "1" or jg_cfg.get("offscreen", False)):
        launch_kwargs["args"].append("--window-position=-3000,-3000")

    if os.path.exists(chrome_exec):
        launch_kwargs["executable_path"] = chrome_exec

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        // Suppress Google One-Tap auto-prompt overlay if present so it doesn't freeze the screen
        window.addEventListener('DOMContentLoaded', () => {
            const style = document.createElement('style');
            style.textContent = '#credential_picker_container, iframe[src*="accounts.google.com/gsi"] { display: none !important; pointer-events: none !important; }';
            document.head?.appendChild(style);
        });
    """)
    return context


def is_logged_in_to_jobgether(page):
    """Check whether the current Jobgether page represents an authenticated session."""
    url = (page.url or "").lower()

    # Clearly unauthenticated if on login or registration path
    if any(k in url for k in ("/login", "/signin", "/sign-in", "/register", "/signup", "/auth")):
        return False

    # Direct URL match for logged-in talent dashboard or candidate area
    if "jobgether.com" in url and any(k in url for k in ("talent", "dashboard", "candidate", "matches", "applications")):
        return True

    # Check cookies for persistent Jobgether session tokens
    try:
        cookies = page.context.cookies()
        if any(c.get("name") in ("token", "jwt", "session", "user_session", "auth_token", "jobgether_session", "sb-access-token", "sb-refresh-token") or "auth" in c.get("name", "").lower() for c in cookies):
            if not any(k in url for k in ("/login", "/signin", "/register")):
                return True
    except Exception:
        pass

    # Look for logged-in selectors
    logged_in_selectors = [
        "text=Welcome back",
        "h1:has-text('Welcome back')",
        "h2:has-text('Welcome back')",
        "div:has-text('Welcome back')",
        "a[href*='/talent']",
        "a[href*='/dashboard']",
        "span:has-text('Dashboard')",
        "a:has-text('Dashboard')",
        "a:has-text('Career Diagnosis')",
        "a:has-text('All Matches')",
        "a:has-text('Applications')",
        "a:has-text('Top Matches')",
        "a[href*='/profile']",
        "a[href*='/logout']",
        "button[aria-label*='user' i]",
        "button[aria-label*='profile' i]",
        "button[aria-label*='account' i]",
        "button:has-text('AK')",
        "div[class*='avatar' i]",
        "div[class*='user-menu' i]",
        "span:has-text('Log out')",
        "span:has-text('Sign out')",
        "a:has-text('Log out')",
        "a:has-text('Sign out')",
        "[data-testid='user-profile']",
        "[data-testid='user-avatar']"
    ]
    for sel in logged_in_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return True
        except Exception:
            pass

    return "jobgether.com" in url and ("talent" in url or "dashboard" in url)


def ensure_logged_in(page, interactive_wait_seconds=300):
    """
    Navigate to Jobgether and verify login.
    If not logged in, wait for the user to log in once in the opened Chrome window.
    """
    logging.info("[Jobgether] Navigating to Jobgether to verify session...")
    try:
        page.goto("https://jobgether.com/", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logging.warning(f"[Jobgether] Initial navigation warning: {e}")

    human_delay(2.5, 4.0)

    # Check if already authenticated on any page/tab
    for p in list(page.context.pages):
        try:
            if is_logged_in_to_jobgether(p):
                logging.info("[Jobgether] Active Jobgether session confirmed!")
                return True
        except Exception:
            pass

    print("\n" + "=" * 65)
    print("  [ACTION REQUIRED] PLEASE LOG IN TO JOBGETHER IN CHROME")
    print("  Google Chrome has opened with the Jobgether portal.")
    print("  Please log in using your Jobgether account (Google SSO, LinkedIn, or Email).")
    print(f"  Waiting up to {interactive_wait_seconds} seconds for login to complete...")
    print("=" * 65 + "\n")
    notify_macos("Jobgether Automation", "Please log in once in the opened Chrome window.", is_urgent=True)

    # Click Sign In / Log In if visible
    try:
        signin_button = page.locator("a:has-text('Sign in'), a:has-text('Log in'), button:has-text('Sign in'), button:has-text('Log in')").first
        if signin_button.count() > 0 and signin_button.is_visible(timeout=2000):
            signin_button.click()
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
                if is_logged_in_to_jobgether(p):
                    logging.info("[Jobgether] Login detected! Saving persistent session.")
                    print("[SUCCESS] Jobgether login detected and saved to persistent Chrome profile!")
                    human_delay(2.0, 3.5)
                    return True
        except Exception:
            pass

    logging.error("[Jobgether] Timed out waiting for Jobgether login.")
    return False


def refresh_jobgether_profile(page, config, ledger, logs_dir):
    """
    Daily Candidate Profile & CV Refresh on Jobgether:
      1. Generates freshly dated Master Resume PDF.
      2. Uploads to candidate profile to reset active candidate freshness timestamp.
    """
    logging.info("[Jobgether] Starting daily profile refresh...")
    try:
        try:
            page.goto("https://jobgether.com/talent/profile", wait_until="domcontentloaded", timeout=30000)
        except Exception:
            page.goto("https://jobgether.com/profile", wait_until="domcontentloaded", timeout=30000)
        human_delay(3.0, 5.0)

        fresh_pdf = naukri_agent.ensure_master_resume_pdf(config)
        refreshed = False

        # Look for resume file inputs
        file_inputs = page.locator("input[type='file'][accept*='pdf' i], input[type='file']")
        if file_inputs.count() > 0:
            file_input = file_inputs.first
            file_input.set_input_files(fresh_pdf)
            human_delay(3.0, 5.0)
            logging.info("[Jobgether] Uploaded freshly dated Master Resume PDF!")
            refreshed = True

        # Save changes button if present
        save_btn = page.locator("button:has-text('Save'), button:has-text('Update'), button[type='submit']").first
        if save_btn.count() > 0 and save_btn.is_visible(timeout=1500):
            save_btn.click()
            human_delay(2.0, 3.0)

        ledger["last_profile_refresh_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ledger["profile_refresh_count"] = ledger.get("profile_refresh_count", 0) + 1
        save_jobgether_ledger(logs_dir, ledger)
        logging.info("[Jobgether] Profile refresh completed successfully.")
        return True
    except Exception as e:
        logging.warning(f"[Jobgether] Profile refresh encountered warning: {e}")
        return False


def extract_and_bridge_emails(job_title, company_name, description_text, config, logs_dir):
    """Search JD text for HR/recruiter emails and dispatch to Dropzone ticket."""
    email_pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
    found_emails = re.findall(email_pattern, description_text)
    valid_emails = []
    
    # Filter out generic domains
    ignore_domains = ['jobgether.com', 'example.com', 'w3.org', 'schema.org', 'sentry.io', 'github.com']
    for em in found_emails:
        em_clean = em.lower().strip()
        domain = em_clean.split('@')[-1]
        if any(ign in domain for ign in ignore_domains):
            continue
        if em_clean not in valid_emails:
            valid_emails.append(em_clean)

    if not valid_emails:
        return []

    logging.info(f"[Jobgether] Extracted HR email(s) for {company_name}: {valid_emails}")
    dropzone_dir = os.path.expanduser(config.get("paths", {}).get("dropzone", "~/Documents/JobAutomation/dropzone"))
    os.makedirs(dropzone_dir, exist_ok=True)

    for em in valid_emails:
        ticket_filename = f"jobgether_{int(time.time())}_{company_name.replace(' ', '_')}.txt"
        ticket_path = os.path.join(dropzone_dir, ticket_filename)
        try:
            with open(ticket_path, "w", encoding="utf-8") as f:
                f.write(f"Company: {company_name}\n")
                f.write(f"Role: {job_title}\n")
                f.write(f"Email: {em}\n")
                f.write(f"Source: Jobgether Remote Job Portal\n")
                f.write(f"\n--- Job Description ---\n")
                f.write(description_text[:3000])
            logging.info(f"[Jobgether] Created Dropzone email ticket -> {ticket_filename}")
        except Exception as e:
            logging.error(f"[Jobgether] Failed to write dropzone ticket: {e}")

    return valid_emails


def apply_to_jobgether_jobs(page, config, ledger, logs_dir, max_applies=None):
    """Search for target remote jobs on Jobgether and execute auto-apply or email extraction."""
    jg_cfg = config.get("jobgether", {})
    daily_limit = jg_cfg.get("daily_apply_limit", 50)
    per_run_limit = jg_cfg.get("apply_limit_per_run", 25)
    target_applies = min(max_applies or per_run_limit, daily_limit)

    applies_today = count_applies_today(ledger)
    remaining_today = max(0, daily_limit - applies_today)
    target_applies = min(target_applies, remaining_today)

    logging.info(f"[Jobgether] Target applies for this run: {target_applies} (Today so far: {applies_today}/{daily_limit})")
    if target_applies <= 0:
        logging.info("[Jobgether] Daily application cap reached. Skipping application run.")
        return 0

    search_cfg = jg_cfg.get("search", {})
    queries = search_cfg.get("queries", [
        "Java Full Stack Developer",
        "Spring Boot Microservices",
        "Full Stack Developer React Java",
        "Java Backend Developer AWS",
        "Python FastAPI Developer"
    ])
    excluded_keywords = search_cfg.get("excluded_title_keywords", [
        ".net", "c#", "php", "wordpress", "salesforce", "sap", "manual testing",
        "qa automation", "sdet", "director", "vp of", "lead architect"
    ])
    max_min_exp = search_cfg.get("max_min_experience_allowed", 5)
    default_pitch = search_cfg.get("default_pitch_note", "")

    applied_count = 0
    applied_records = ledger.setdefault("applied_jobs", {})

    for query in queries:
        if applied_count >= target_applies:
            break

        logging.info(f"[Jobgether] Searching jobs for query: '{query}'...")
        encoded_query = query.replace(" ", "+")
        search_url = f"https://jobgether.com/jobs?keyword={encoded_query}&locations=anywhere"

        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            human_delay(3.0, 5.0)
        except Exception as e:
            logging.warning(f"[Jobgether] Search navigation warning for '{query}': {e}")
            continue

        # Look for job card listings
        job_card_selectors = [
            "div[class*='job-card' i]",
            "div[class*='jobCard' i]",
            "article[class*='job' i]",
            "div[class*='listing' i]",
            "a[href*='/job/']",
            "a[href*='/offer/']"
        ]

        job_cards = []
        for sel in job_card_selectors:
            try:
                cards = page.locator(sel)
                if cards.count() > 0:
                    job_cards = cards
                    break
            except Exception:
                pass

        card_count = job_cards.count() if job_cards else 0
        logging.info(f"[Jobgether] Found {card_count} potential listings for query '{query}'")

        for idx in range(card_count):
            if applied_count >= target_applies:
                break

            try:
                card = job_cards.nth(idx)
                if not card.is_visible():
                    continue

                card_text = card.inner_text()
                lines = [l.strip() for l in card_text.split("\n") if l.strip()]
                if not lines:
                    continue

                title = lines[0]
                company = lines[1] if len(lines) > 1 else "Unknown Company"

                # Check excluded title keywords
                title_lower = title.lower()
                if any(excl.lower() in title_lower for excl in excluded_keywords):
                    continue

                # Experience check in card text
                exp_match = re.search(r'(\d+)\+?\s*(?:-\s*(\d+))?\s*(?:years?|yrs?)', card_text, re.IGNORECASE)
                if exp_match:
                    min_req = int(exp_match.group(1))
                    if min_req > max_min_exp:
                        continue

                # Deduplication key
                dedup_key = f"{title.lower()}::{company.lower()}"
                if dedup_key in applied_records:
                    continue

                # Click into job card / detail
                card.scroll_into_view_if_needed()
                human_delay(0.8, 1.5)
                card.click()
                human_delay(2.0, 3.5)

                # Get description text
                desc_text = ""
                desc_loc = page.locator("div[class*='description' i], div[class*='job-detail' i], div[class*='details' i]").first
                if desc_loc.count() > 0:
                    try:
                        desc_text = desc_loc.inner_text()
                    except Exception:
                        pass

                # Check if HR email bridge is enabled
                extracted_emails = []
                if search_cfg.get("extract_hr_emails_to_dropzone", True) and desc_text:
                    extracted_emails = extract_and_bridge_emails(title, company, desc_text, config, logs_dir)

                # Look for 1-Click / Easy Apply button
                apply_btn = page.locator(
                    "button:has-text('Apply'), a:has-text('Apply'), "
                    "button:has-text('Quick Apply'), button:has-text('1-Click Apply'), "
                    "button[class*='apply' i]"
                ).first

                if apply_btn.count() > 0 and apply_btn.is_visible(timeout=1500):
                    apply_text = apply_btn.inner_text()
                    # Click apply button
                    apply_btn.click()
                    human_delay(2.0, 3.5)

                    # Handle pitch note modal if present
                    pitch_box = page.locator("textarea[placeholder*='pitch' i], textarea[placeholder*='note' i], textarea[placeholder*='cover' i], textarea").first
                    if pitch_box.count() > 0 and pitch_box.is_visible(timeout=1500):
                        pitch_box.fill(default_pitch)
                        human_delay(1.0, 2.0)

                    # Submit button inside modal
                    submit_btn = page.locator("button:has-text('Submit'), button:has-text('Send application'), button:has-text('Confirm')").first
                    if submit_btn.count() > 0 and submit_btn.is_visible(timeout=1500):
                        submit_btn.click()
                        human_delay(2.0, 3.0)

                    applied_count += 1
                    applied_records[dedup_key] = {
                        "title": title,
                        "company": company,
                        "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "APPLIED",
                        "query": query,
                        "source": "jobgether"
                    }
                    save_jobgether_ledger(logs_dir, ledger)
                    logging.info(f"✅ Applied ({applied_count}/{target_applies}): '{title}' at '{company}'")
                    notify_macos("Jobgether Auto-Apply", f"Submitted application: {title} at {company}")
                elif extracted_emails:
                    applied_records[dedup_key] = {
                        "title": title,
                        "company": company,
                        "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "HR_EXTRACTED",
                        "emails": extracted_emails,
                        "query": query,
                        "source": "jobgether"
                    }
                    save_jobgether_ledger(logs_dir, ledger)
                    logging.info(f"📧 HR Emails bridged to Dropzone for '{title}' at '{company}': {extracted_emails}")

                human_delay(1.5, 3.0)

            except Exception as e:
                logging.warning(f"[Jobgether] Error processing listing #{idx}: {e}")
                continue

    return applied_count


def run_jobgether_pipeline(mode="apply", max_applies=None, force_headed=False):
    """Entry point for Jobgether automation pipeline."""
    config = email_agent.load_config()
    jg_cfg = config.get("jobgether", {})
    if not jg_cfg.get("enabled", True):
        logging.info("[Jobgether] Jobgether agent is disabled in config.json.")
        return

    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_jobgether_ledger(logs_dir)

    needs_initial_login = not ledger.get("login_verified", False)
    is_headed = force_headed or (mode == "login") or needs_initial_login

    try:
        with sync_playwright() as playwright:
            context = launch_jobgether_browser(playwright, config, force_headed=is_headed)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.bring_to_front()
            except Exception:
                pass

            try:
                wait_secs = 300 if (mode == "login" or needs_initial_login) else 30
                logged_in = ensure_logged_in(page, interactive_wait_seconds=wait_secs)
                if not logged_in:
                    logging.error("[Jobgether] Authentication required. Run with --login-jobgether to authenticate.")
                    print("[Jobgether] Authentication required. Please run: python3 run_all.py --login-jobgether")
                    return

                ledger["login_verified"] = True
                save_jobgether_ledger(logs_dir, ledger)

                if mode == "login":
                    logging.info("[Jobgether] Login verification complete! Persistent profile is ready.")
                    print("\n[SUCCESS] Jobgether session verified and persistent Chrome profile is saved!\n")
                    return

                if mode in ("refresh", "all"):
                    refresh_jobgether_profile(page, config, ledger, logs_dir)

                if mode in ("apply", "all"):
                    apply_to_jobgether_jobs(page, config, ledger, logs_dir, max_applies=max_applies)

            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[Jobgether] Execution cancelled by user.")
        return


def main():
    parser = argparse.ArgumentParser(description="Jobgether Automation Agent")
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
        run_jobgether_pipeline(mode=args.mode, max_applies=args.max_applies, force_headed=force_headed)


if __name__ == "__main__":
    main()
