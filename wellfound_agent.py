#!/usr/bin/env python3
"""
Wellfound (formerly AngelList Talent) Daily Profile Refresher & 1-Click Startup Applier
Integrates with agent.py, config.json, and persistent Chrome browser session.
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


def human_delay(min_sec=1.5, max_sec=3.5):
    """Sleep for a randomized human-like interval."""
    time.sleep(random.uniform(min_sec, max_sec))


def notify_macos(title, message, is_urgent=False):
    """Display a non-blocking macOS notification banner respecting quiet_mode."""
    power_utils.notify_macos(title, message, is_urgent=is_urgent)


def load_wellfound_ledger(logs_dir):
    """Load persistent ledger of applied Wellfound opportunities and profile refresh state."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'wellfound_applied_jobs.json')
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
            logging.warning(f"[Wellfound] Could not read wellfound_applied_jobs.json: {e}")
    return default_data


def save_wellfound_ledger(logs_dir, ledger):
    """Persist Wellfound applied jobs and profile state."""
    ledger_path = os.path.join(logs_dir, 'wellfound_applied_jobs.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"[Wellfound] Could not save wellfound_applied_jobs.json: {e}")


def count_applies_today(ledger):
    """Count how many Wellfound opportunities were successfully applied to today (YYYY-MM-DD)."""
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


def launch_wellfound_browser(playwright, config, force_headed=False):
    """Launch a persistent Google Chrome context dedicated to Wellfound automation."""
    wf_cfg = config.get('wellfound', {})
    profile_dir = os.path.expanduser(
        wf_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/wellfound')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = wf_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(wf_cfg.get('headless', False))
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
    if not force_headed and (os.environ.get("JOB_AUTO_OFFSCREEN") == "1" or wf_cfg.get("offscreen", False)):
        launch_kwargs["args"].append("--window-position=-3000,-3000")

    if os.path.exists(chrome_exec):
        launch_kwargs["executable_path"] = chrome_exec

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    return context


def is_logged_in_to_wellfound(page):
    """Check whether the current Wellfound page represents an authenticated session."""
    url = (page.url or "").lower()
    if "login" in url or "join" in url or "users/sign_in" in url:
        return False

    login_form_selectors = [
        "input[name='user[email]']",
        "input#user_email",
        "input[type='password']",
        "button:has-text('Log In')",
        "button:has-text('Sign In')",
        "a[href*='/login']",
        "a[href*='/users/sign_in']",
        "a:has-text('Log In')",
        "button:has-text('Log In')",
        "a:has-text('Sign Up')",
        "button:has-text('Sign Up')"
    ]
    for sel in login_form_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                return False
        except Exception:
            pass

    logged_in_selectors = [
        "button[aria-label*='user menu' i]",
        "button[aria-label*='profile' i]",
        "a[href*='/logout']",
        "a[href*='/profile/edit']",
        "[data-test='navigation-profile-link']",
        ".styles_userMenu__",
        "img[alt*='avatar' i]"
    ]
    for sel in logged_in_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return True
        except Exception:
            pass

    return False


def ensure_logged_in(page, interactive_wait_seconds=300):
    """
    Navigate to Wellfound and verify login.
    If not logged in, wait for the user to log in once in the opened Chrome window.
    """
    logging.info("[Wellfound] Navigating to Wellfound to verify session...")
    try:
        page.goto("https://wellfound.com/jobs", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logging.warning(f"[Wellfound] Initial navigation warning: {e}")

    human_delay(2.5, 4.0)

    # Check if already authenticated on any page/tab
    for p in list(page.context.pages):
        try:
            if is_logged_in_to_wellfound(p):
                logging.info("[Wellfound] Active Wellfound session confirmed!")
                return True
        except Exception:
            pass

    print("\n" + "=" * 65)
    print("  [ACTION REQUIRED] PLEASE LOG IN TO WELLFOUND IN CHROME")
    print("  Google Chrome has opened with the Wellfound login page.")
    print("  Please log in using your Wellfound / AngelList credentials.")
    print(f"  Waiting up to {interactive_wait_seconds} seconds for login to complete...")
    print("=" * 65 + "\n")
    notify_macos("Wellfound Automation", "Please log in once in the opened Chrome window.", is_urgent=True)

    try:
        if "wellfound.com" not in (page.url or "") or "login" not in (page.url or ""):
            page.goto("https://wellfound.com/login", wait_until="domcontentloaded", timeout=30000)
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
                if is_logged_in_to_wellfound(p):
                    logging.info("[Wellfound] Login detected! Saving persistent session.")
                    print("[SUCCESS] Wellfound login detected and saved to persistent Chrome profile!")
                    human_delay(1.5, 3.0)
                    return True
        except Exception:
            pass

    logging.error("[Wellfound] Timed out waiting for Wellfound login.")
    return False


def refresh_wellfound_profile(page, config, ledger, logs_dir):
    """
    Touch Wellfound profile / jobs feed to register candidate activity.
    """
    logging.info("[Wellfound] Starting daily candidate activity refresh...")
    try:
        page.goto("https://wellfound.com/jobs", wait_until="domcontentloaded", timeout=45000)
        human_delay(2.5, 4.0)
        page.evaluate("window.scrollTo(0, 500)")
        human_delay(1.5, 2.5)

        ledger["last_profile_refresh_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ledger["profile_refresh_count"] = int(ledger.get("profile_refresh_count", 0)) + 1
        save_wellfound_ledger(logs_dir, ledger)
        logging.info("[Wellfound] Profile refreshed successfully!")
        return True
    except Exception as e:
        logging.warning(f"[Wellfound] Profile refresh warning: {e}")
        return False


def is_job_suitable(title, snippet, config):
    """Check if the role matches Amrit's tech stack and does not contain excluded keywords."""
    wf_cfg = config.get("wellfound", {})
    search_cfg = wf_cfg.get("search", {})
    excluded = search_cfg.get("excluded_title_keywords", [
        ".net", "c#", "php", "wordpress", "salesforce", "sap", "manual testing", "qa automation", "sdet", "director", "vp of"
    ])
    combined_text = f"{title} {snippet}".lower()
    for ex in excluded:
        if ex.lower() in combined_text:
            return False, f"Contains excluded keyword '{ex}'"

    # Match positive tech keywords
    tech_keywords = ["java", "spring", "microservices", "react", "fastapi", "python", "backend", "full stack", "fullstack", "software engineer"]
    has_tech = any(tk in combined_text for tk in tech_keywords)
    if not has_tech:
        return False, "Does not match target tech stack (Java/Spring/React/FastAPI/Backend)"

    return True, "Role is suitable"


def bridge_hr_emails_to_dropzone(jd_text, title, company, job_id, config):
    """Extract HR emails from JD text and create a ticket in Dropzone for agent.py."""
    wf_cfg = config.get("wellfound", {})
    if not wf_cfg.get("search", {}).get("extract_hr_emails_to_dropzone", True):
        return []

    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', jd_text or "")
    valid_emails = [e for e in emails if not any(x in e.lower() for x in ("support@", "info@", "help@", "wellfound.com", "angellist.com"))]
    if not valid_emails:
        return []

    dropzone_dir = email_agent.get_dropzone_path(config)
    os.makedirs(dropzone_dir, exist_ok=True)
    filename = f"wellfound_{re.sub(r'[^a-zA-Z0-9_]', '_', company)}_{job_id}.txt"
    file_path = os.path.join(dropzone_dir, filename)

    content = f"""SOURCE: Wellfound
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
        logging.info(f"[Wellfound -> Email Bridge] Dropped HR email ticket: {filename} ({valid_emails})")
    except Exception as e:
        logging.warning(f"[Wellfound -> Email Bridge] Failed to write dropzone file: {e}")

    return valid_emails


def apply_to_wellfound_jobs(page, config, ledger, logs_dir, max_applies=None):
    """
    Scan Wellfound jobs matching queries and apply with a customized pitch note.
    """
    wf_cfg = config.get("wellfound", {})
    search_cfg = wf_cfg.get("search", {})
    queries = search_cfg.get("queries", ["Java Full Stack Developer", "Spring Boot Microservices", "Full Stack Developer React"])
    daily_limit = wf_cfg.get("daily_apply_limit", 40)
    per_run_limit = wf_cfg.get("apply_limit_per_run", 20)
    pitch_note = search_cfg.get("default_pitch_note", (
        "Hi, I am Amrit Kumar, a Full Stack Developer with 3.5 years of experience building scalable applications using "
        "Java 21, Spring Boot 3, Microservices, React 19, Python FastAPI, PostgreSQL, and AWS. "
        "Immediate joiner (7 days notice period) and excited to contribute to high-impact product engineering."
    ))

    applied_today = count_applies_today(ledger)
    remaining_daily = max(0, daily_limit - applied_today)
    if remaining_daily <= 0:
        logging.info(f"[Wellfound] Daily apply limit reached ({applied_today}/{daily_limit}).")
        print(f"[Wellfound] Daily apply limit reached ({applied_today}/{daily_limit}).")
        return 0

    target_applies = min(remaining_daily, per_run_limit)
    if max_applies is not None:
        target_applies = min(target_applies, int(max_applies))

    logging.info(f"[Wellfound] Target applies for this run: {target_applies} (Today so far: {applied_today}/{daily_limit})")

    applied_count = 0
    applied_records = ledger.get("applied_jobs", {})

    for query in queries:
        if applied_count >= target_applies:
            break

        logging.info(f"[Wellfound] Searching jobs for query: '{query}'...")
        encoded_q = quote_plus(query)
        search_url = f"https://wellfound.com/jobs?query={encoded_q}"
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            human_delay(3.0, 5.0)
        except Exception as e:
            logging.warning(f"[Wellfound] Search navigation warning for '{query}': {e}")
            continue

        # Dismiss any welcome overlay or lingering modal
        try:
            page.keyboard.press("Escape")
            continue_btn = page.locator('.ReactModalPortal button:has-text("Continue"), button:has-text("Continue")').first
            if continue_btn.count() > 0 and continue_btn.is_visible(timeout=1000):
                continue_btn.click(timeout=1500)
                human_delay(1.0, 2.0)
        except Exception:
            pass

        # Scroll to load listings
        for _ in range(3):
            page.evaluate("window.scrollBy(0, 800)")
            human_delay(1.0, 2.0)

        # Locate job cards / apply buttons
        job_card_selectors = [
            "[data-test='JobListing']",
            ".styles_jobListing__",
            "div[class*='jobListing']",
            "div[class*='JobCard']",
            "div[class*='listing-item']"
        ]
        job_cards = None
        for sel in job_card_selectors:
            loc = page.locator(sel)
            if loc.count() > 0:
                job_cards = loc
                break

        if not job_cards or job_cards.count() == 0:
            logging.info(f"[Wellfound] No job card elements found with specific selector, scanning buttons...")
            apply_buttons = page.locator("button:has-text('Apply'), a:has-text('Apply')")
            card_count = apply_buttons.count()
        else:
            card_count = job_cards.count()

        logging.info(f"[Wellfound] Found {card_count} potential listings for query '{query}'")

        for idx in range(card_count):
            if applied_count >= target_applies:
                break

            try:
                card = job_cards.nth(idx) if job_cards else None
                card_text = (card.inner_text(timeout=2000) or "") if card else ""
                lines = [l.strip() for l in card_text.splitlines() if l.strip()]
                filtered_lines = [l for l in lines if not any(b in l.lower() for b in ("apply on wellfound", "promoted", "featured", "recruiter recently active", "posted", "save", "learn more"))]
                title = filtered_lines[0] if filtered_lines else "Software Engineer"
                company = filtered_lines[1] if len(filtered_lines) > 1 else "Startup"

                job_id = f"wf_{abs(hash(company + title)) % 10000000}"
                if job_id in applied_records and applied_records[job_id].get("status") == "APPLIED":
                    continue

                suitable, reason = is_job_suitable(title, card_text, config)
                if not suitable:
                    continue

                # Check for HR email in text
                bridge_hr_emails_to_dropzone(card_text, title, company, job_id, config)

                # Locate role link to navigate directly to role page
                role_link = card.locator("a[href*='/jobs/'], a[href*='/role/'], a[href*='/l/']").first if card else None
                if not role_link or role_link.count() == 0:
                    continue

                href = role_link.get_attribute("href") or ""
                role_url = "https://wellfound.com" + href if href.startswith('/') else href

                logging.info(f"[Wellfound] Opening role page: {role_url} for '{title}' at '{company}'...")
                try:
                    page.goto(role_url, wait_until="domcontentloaded", timeout=25000)
                    human_delay(2.0, 3.5)
                    page.keyboard.press("Escape")
                except Exception as e:
                    logging.warning(f"[Wellfound] Navigation to role failed: {e}")
                    continue

                # Deep JD inspection
                deep_pane = page.locator("div[class*='jobDescription'], div[class*='description'], div[data-test*='description']").first
                if deep_pane.count() > 0:
                    try:
                        deep_text = deep_pane.inner_text(timeout=2000) or ""
                        if deep_text:
                            bridge_hr_emails_to_dropzone(deep_text, title, company, job_id, config)
                    except Exception:
                        pass

                detail_apply_btn = page.locator("button:has-text('Apply'), a:has-text('Apply'), [data-test='apply-button']").first
                if detail_apply_btn.count() == 0 or not detail_apply_btn.is_visible(timeout=3000):
                    logging.info(f"[Wellfound] No visible Apply CTA in detail page for '{title}'. Skipping.")
                    page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                    human_delay(1.5, 2.5)
                    continue

                btn_text = (detail_apply_btn.inner_text(timeout=500) or "").strip().lower()
                if "applied" in btn_text:
                    applied_records[job_id] = {"company": company, "title": title, "status": "ALREADY_APPLIED"}
                    page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                    human_delay(1.5, 2.5)
                    continue

                logging.info(f"[Wellfound] Clicking Apply on role page for '{title}' at '{company}'...")
                page.keyboard.press("Escape")
                detail_apply_btn.click(force=True, timeout=3000)
                human_delay(2.0, 3.5)

                # If modal opens asking for pitch note
                note_textarea = page.locator("textarea[name='note'], textarea[placeholder*='note' i], textarea[placeholder*='pitch' i], textarea").first
                if note_textarea.count() > 0 and note_textarea.is_visible(timeout=2000):
                    try:
                        note_textarea.fill(pitch_note, timeout=3000)
                        human_delay(1.0, 2.0)
                    except Exception:
                        pass

                # Submit button inside modal
                send_btn = page.locator("button:has-text('Send application'), button:has-text('Submit application'), button:has-text('Send note'), button.styles_button__primary").first
                if send_btn.count() > 0 and send_btn.is_visible(timeout=2000):
                    send_btn.click(force=True, timeout=4000)
                    human_delay(2.0, 3.5)

                applied_records[job_id] = {
                    "company": company,
                    "title": title,
                    "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "APPLIED",
                    "channel": "Wellfound"
                }
                applied_count += 1
                save_wellfound_ledger(logs_dir, ledger)
                logging.info(f"[Wellfound] Successfully applied to '{title}' at '{company}' ({applied_count}/{target_applies})")
                print(f"[Wellfound] Successfully applied: '{title}' at '{company}'")

                # Navigate back to search page to resume listing scan
                page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                human_delay(2.0, 3.5)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass

                human_delay(2.0, 3.5)
            except Exception as e:
                logging.warning(f"[Wellfound] Error processing listing #{idx}: {e}")
                continue

    logging.info(f"[Wellfound] Search & apply scanning finished. Applied to {applied_count} startup roles today.")
    save_wellfound_ledger(logs_dir, ledger)
    notify_macos("Wellfound Auto-Applier", f"Finished run: Applied to {applied_count} startup roles today.")
    return applied_count


def run_wellfound_pipeline(mode="apply", max_applies=None, force_headed=False):
    """Entry point for Wellfound automation pipeline."""
    config = email_agent.load_config()
    wf_cfg = config.get("wellfound", {})
    if not wf_cfg.get("enabled", True):
        logging.info("[Wellfound] Wellfound agent is disabled in config.json.")
        return

    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_wellfound_ledger(logs_dir)

    needs_initial_login = not ledger.get("login_verified", False)
    is_headed = force_headed or (mode == "login") or needs_initial_login

    try:
        with sync_playwright() as playwright:
            context = launch_wellfound_browser(playwright, config, force_headed=is_headed)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.bring_to_front()
            except Exception:
                pass

            try:
                wait_secs = 300 if (mode == "login" or needs_initial_login) else 30
                logged_in = ensure_logged_in(page, interactive_wait_seconds=wait_secs)
                if not logged_in:
                    logging.error("[Wellfound] Authentication required. Run with --login-wellfound to authenticate.")
                    print("[Wellfound] Authentication required. Please run: python3 run_all.py --login-wellfound")
                    return

                ledger["login_verified"] = True
                save_wellfound_ledger(logs_dir, ledger)

                if mode == "login":
                    logging.info("[Wellfound] Login verification complete! Persistent profile is ready.")
                    print("\n[SUCCESS] Wellfound session verified and persistent Chrome profile is saved!\n")
                    return

                if mode in ("refresh", "all"):
                    refresh_wellfound_profile(page, config, ledger, logs_dir)

                if mode in ("apply", "all"):
                    apply_to_wellfound_jobs(page, config, ledger, logs_dir, max_applies=max_applies)

            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[Wellfound] Execution cancelled by user.")
        return


def main():
    parser = argparse.ArgumentParser(description="Wellfound Automation Agent")
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
        run_wellfound_pipeline(mode=args.mode, max_applies=args.max_applies, force_headed=force_headed)


if __name__ == "__main__":
    main()
