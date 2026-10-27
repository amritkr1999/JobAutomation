#!/usr/bin/env python3
"""
Shine.com Daily Profile Refresher & 1-Click Auto-Applier
Integrates with agent.py, config.json, naukri_agent QA bank, and persistent Chrome browser session.
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


def load_shine_ledger(logs_dir):
    """Load persistent ledger of applied Shine jobs and profile refresh state."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'shine_applied_jobs.json')
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
            logging.warning(f"[Shine] Could not read shine_applied_jobs.json: {e}")
    return default_data


def save_shine_ledger(logs_dir, ledger):
    """Persist Shine applied jobs and profile refresh state."""
    ledger_path = os.path.join(logs_dir, 'shine_applied_jobs.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"[Shine] Could not save shine_applied_jobs.json: {e}")


def count_applies_today(ledger):
    """Count how many Shine jobs were successfully applied to today (YYYY-MM-DD)."""
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


def launch_shine_browser(playwright, config, force_headed=False):
    """Launch a persistent Google Chrome context dedicated to Shine automation."""
    sh_cfg = config.get('shine', {})
    profile_dir = os.path.expanduser(
        sh_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/shine')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = sh_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(sh_cfg.get('headless', False))
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
    if not force_headed and (os.environ.get("JOB_AUTO_OFFSCREEN") == "1" or sh_cfg.get("offscreen", False)):
        launch_kwargs["args"].append("--window-position=-3000,-3000")

    if os.path.exists(chrome_exec):
        launch_kwargs["executable_path"] = chrome_exec

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    return context


def is_logged_in_to_shine(page):
    """Check whether the current Shine page represents an authenticated session."""
    # 1. Primary check: Modern Shine JWT & Candidate profile in localStorage
    try:
        has_auth = page.evaluate("""() => {
            try {
                const access = localStorage.getItem('access');
                const userData = localStorage.getItem('userData');
                if (access && access.length > 20) return true;
                if (userData && (userData.includes('candidate_id') || userData.includes('name'))) return true;
            } catch (e) {}
            return false;
        }""")
        if has_auth:
            return True
    except Exception:
        pass

    # 2. Check cookies for authenticated session tokens
    try:
        cookies = page.context.cookies()
        if any(c.get("name") in ("_userid_", "_cpem_", "user_id", "sessionid", "proxy_sessionid") and c.get("value") for c in cookies):
            return True
    except Exception:
        pass

    # 3. Negative URL check (only if neither storage nor cookies confirmed auth)
    url = (page.url or "").lower()
    if any(k in url for k in ("myshine/login", "registration")):
        return False

    # 4. Positive authenticated DOM indicators
    cand_name = config.get("profile", {}).get("full_name", "")
    authenticated_selectors = [
        "a:has-text('Update Profile')",
        "span:has-text('Update Profile')",
        "div:has-text('Recruiters Activity')",
        "div:has-text('Search Appearance')",
        "span:has-text('My Profile')",
        "div.profile_pic",
        "a[href*='/logout']",
        "a:has-text('Logout')",
        "a:has-text('My Account')",
        "div[class*='user_profile']",
        "div[class*='myShine']"
    ]
    if cand_name:
        authenticated_selectors.extend([
            f"h2:has-text('{cand_name}')",
            f"span:has-text('{cand_name}')",
            f"text={cand_name}"
        ])
    for sel in authenticated_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=300):
                return True
        except Exception:
            pass

    return False


def ensure_logged_in(page, interactive_wait_seconds=300):
    """
    Navigate to Shine and verify login.
    If not logged in, wait for the user to log in once in the opened Chrome window.
    """
    logging.info("[Shine] Navigating to Shine to verify session...")
    try:
        page.goto("https://www.shine.com/myshine/mydashboard/", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logging.warning(f"[Shine] Initial navigation warning: {e}")

    human_delay(2.5, 4.0)

    # Check if already authenticated on any page/tab
    for p in list(page.context.pages):
        try:
            if is_logged_in_to_shine(p):
                logging.info("[Shine] Active Shine session confirmed!")
                return True
        except Exception:
            pass

    print("\n" + "=" * 65)
    print("  [ACTION REQUIRED] PLEASE LOG IN TO SHINE.COM IN CHROME")
    print("  Google Chrome has opened with the Shine login page.")
    print("  Please log in using your Shine account credentials (or Google SSO).")
    print(f"  Waiting up to {interactive_wait_seconds} seconds for login to complete...")
    print("=" * 65 + "\n")
    notify_macos("Shine Automation", "Please log in once in the opened Chrome window.", is_urgent=True)

    try:
        if "shine.com/myshine/login" not in (page.url or ""):
            page.goto("https://www.shine.com/myshine/login/", wait_until="domcontentloaded", timeout=30000)
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
                if is_logged_in_to_shine(p):
                    logging.info("[Shine] Login detected! Saving persistent session.")
                    print("[SUCCESS] Shine login detected and saved to persistent Chrome profile!")
                    human_delay(1.5, 3.0)
                    return True
        except Exception:
            pass

    logging.error("[Shine] Timed out waiting for Shine login.")
    return False


def refresh_shine_profile(page, config, ledger, logs_dir):
    """
    Daily Profile Refresher on Shine:
      1. Generates freshly dated Master Resume PDF.
      2. Uploads via profile resume uploader to reset candidate freshness timestamp.
    """
    logging.info("[Shine] Starting daily profile refresh...")
    try:
        page.goto("https://www.shine.com/myshine/myprofile/", wait_until="domcontentloaded", timeout=45000)
        human_delay(3.0, 5.0)

        # Generate fresh master resume PDF
        fresh_pdf = naukri_agent.ensure_master_resume_pdf(config)
        refreshed = False

        # Look for resume file input
        file_inputs = page.locator("input[type='file'][id*='resume' i], input[type='file'], #id_resume_file")
        if file_inputs.count() > 0:
            file_input = file_inputs.first
            file_input.set_input_files(fresh_pdf)
            human_delay(2.5, 4.0)
            logging.info("[Shine] Uploaded fresh Master Resume PDF!")
            refreshed = True

        # Click save / update if present
        save_btn = page.locator("button:has-text('Update'), button:has-text('Save'), input[value='Update']").first
        if save_btn.count() > 0 and save_btn.is_visible(timeout=1500):
            save_btn.click(timeout=3000)
            human_delay(2.0, 3.0)
            refreshed = True

        if refreshed:
            ledger["last_profile_refresh_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ledger["profile_refresh_count"] = int(ledger.get("profile_refresh_count", 0)) + 1
            save_shine_ledger(logs_dir, ledger)
            logging.info("[Shine] Daily Profile Refresh completed successfully!")
            return True
        else:
            logging.warning("[Shine] Profile refresh completed without explicit file upload trigger.")
            return False
    except Exception as e:
        logging.warning(f"[Shine] Profile refresh error: {e}")
        return False


def is_job_suitable(title, snippet, config):
    """Check if the role matches Amrit's tech stack and does not contain excluded keywords."""
    sh_cfg = config.get("shine", {})
    search_cfg = sh_cfg.get("search", {})
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
    sh_cfg = config.get("shine", {})
    if not sh_cfg.get("search", {}).get("extract_hr_emails_to_dropzone", True):
        return []

    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', jd_text or "")
    valid_emails = [e for e in emails if not any(x in e.lower() for x in ("support@", "info@", "help@", "shine.com"))]
    if not valid_emails:
        return []

    dropzone_dir = email_agent.get_dropzone_path(config)
    os.makedirs(dropzone_dir, exist_ok=True)
    filename = f"shine_{re.sub(r'[^a-zA-Z0-9_]', '_', company)}_{job_id}.txt"
    file_path = os.path.join(dropzone_dir, filename)

    content = f"""SOURCE: Shine.com
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
        logging.info(f"[Shine -> Email Bridge] Dropped HR email ticket: {filename} ({valid_emails})")
    except Exception as e:
        logging.warning(f"[Shine -> Email Bridge] Failed to write dropzone file: {e}")

    return valid_emails


def apply_to_shine_jobs(page, config, ledger, logs_dir, max_applies=None):
    """
    Search Shine.com across target queries and execute 1-Click apply.
    """
    sh_cfg = config.get("shine", {})
    search_cfg = sh_cfg.get("search", {})
    queries = search_cfg.get("queries", ["Java Full Stack Developer", "Spring Boot Microservices Developer", "Java Backend Developer"])
    daily_limit = sh_cfg.get("daily_apply_limit", 50)
    per_run_limit = sh_cfg.get("apply_limit_per_run", 25)

    applied_today = count_applies_today(ledger)
    remaining_daily = max(0, daily_limit - applied_today)
    if remaining_daily <= 0:
        logging.info(f"[Shine] Daily apply limit reached ({applied_today}/{daily_limit}).")
        print(f"[Shine] Daily apply limit reached ({applied_today}/{daily_limit}).")
        return 0

    target_applies = min(remaining_daily, per_run_limit)
    if max_applies is not None:
        target_applies = min(target_applies, int(max_applies))

    logging.info(f"[Shine] Target applies for this run: {target_applies} (Today so far: {applied_today}/{daily_limit})")

    applied_count = 0
    applied_records = ledger.get("applied_jobs", {})

    for query in queries:
        if applied_count >= target_applies:
            break

        slug = re.sub(r'\s+', '-', query.strip().lower())
        search_url = f"https://www.shine.com/job-search/{slug}-jobs"

        logging.info(f"[Shine] Searching: '{query}' -> {search_url}")
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            human_delay(3.0, 5.0)
        except Exception as e:
            logging.warning(f"[Shine] Search navigation warning: {e}")
            continue

        # Check for job cards with expanded modern selectors
        card_selectors = (
            "article[class*='result-card_card'], article[class*='result-card'], "
            "div.jobCard, div[class*='jobCard'], div[class*='parent_job_card'], "
            "div[class*='job_card'], div.search_listing, div[class*='srpTuple'], "
            "div[class*='job-tuple'], div[class*='cardContainer']"
        )
        cards = page.locator(card_selectors)
        card_count = cards.count()

        # If zero cards found with slug URL, attempt standard search parameter URL
        if card_count == 0:
            alt_url = f"https://www.shine.com/job-search?q={quote_plus(query)}"
            try:
                logging.info(f"[Shine] Retrying with query parameter URL -> {alt_url}")
                page.goto(alt_url, wait_until="domcontentloaded", timeout=35000)
                human_delay(3.0, 4.5)
                cards = page.locator(card_selectors)
                card_count = cards.count()
            except Exception:
                pass

        logging.info(f"[Shine] Found {card_count} job cards for '{query}'")

        for idx in range(card_count):
            if applied_count >= target_applies:
                break

            try:
                card = cards.nth(idx)
                card.scroll_into_view_if_needed(timeout=2000)
                card_text = (card.inner_text(timeout=2000) or "")

                # Extract title and company
                title_elem = card.locator("a[class*='result-card_hit'], h3 a, a[href*='/jobs/'], h2 a, strong a, .job_title_anchor, a[class*='jobTitle'], a[class*='title']").first
                title = (title_elem.inner_text(timeout=1000) or "").strip() if title_elem.count() > 0 else "Software Engineer"
                company_elem = card.locator("span[class*='result-card_company'], div[class*='result-card_company'], .jobCard_jobCard_cName__mYnow, span[class*='cName'], .company_name, a[class*='company']").first
                company = (company_elem.inner_text(timeout=1000) or "").strip() if company_elem.count() > 0 else "Tech Company"

                job_id = f"sh_{abs(hash(company + title)) % 10000000}"
                if job_id in applied_records and applied_records[job_id].get("status") == "APPLIED":
                    continue

                suitable, reason = is_job_suitable(title, card_text, config)
                if not suitable:
                    continue

                # Bridge any HR email in text to Dropzone
                bridge_hr_emails_to_dropzone(card_text, title, company, job_id, config)

                # Locate Apply button on card or click through to detail view
                apply_btn = card.locator("button:has-text('Apply'), .apply_btn, a.cls_apply, button[id*='apply'], a:has-text('Apply')").first
                target_page = page
                is_popup = False
                submitted = False

                if apply_btn.count() > 0 and apply_btn.is_visible(timeout=1000):
                    btn_text = (apply_btn.inner_text(timeout=500) or "").strip().lower()
                    if "applied" in btn_text:
                        applied_records[job_id] = {"company": company, "title": title, "status": "ALREADY_APPLIED"}
                        continue
                    logging.info(f"[Shine] Applying directly from card: '{title}' at '{company}'...")
                    apply_btn.click(timeout=3000)
                    human_delay(2.0, 3.5)
                    submitted = True
                else:
                    # Click into card / title to open detail pane or new tab
                    clickable = title_elem if (title_elem.count() > 0 and title_elem.is_visible(timeout=1000)) else card
                    logging.info(f"[Shine] Clicking into job detail view for: '{title}' at '{company}'...")
                    try:
                        with page.expect_popup(timeout=2500) as popup_info:
                            clickable.click(timeout=3000)
                        target_page = popup_info.value
                        is_popup = True
                        target_page.wait_for_load_state("domcontentloaded", timeout=15000)
                        human_delay(2.0, 3.5)
                    except Exception:
                        target_page = page
                        human_delay(1.5, 3.0)

                    detail_apply_btn = target_page.locator("button:has-text('Apply'), a:has-text('Apply'), button[id*='apply']").first
                    if detail_apply_btn.count() == 0 or not detail_apply_btn.is_visible(timeout=2000):
                        if is_popup and target_page:
                            try:
                                target_page.close()
                            except Exception:
                                pass
                        continue

                    btn_text = (detail_apply_btn.inner_text(timeout=500) or "").strip().lower()
                    if "applied" in btn_text:
                        applied_records[job_id] = {"company": company, "title": title, "status": "ALREADY_APPLIED"}
                        if is_popup and target_page:
                            try:
                                target_page.close()
                            except Exception:
                                pass
                        continue

                    logging.info(f"[Shine] Clicking Apply inside detail view for '{title}' at '{company}'...")
                    detail_apply_btn.click(timeout=3000)
                    human_delay(2.0, 3.5)
                    submitted = True

                # Check if genuinely redirected to unauthenticated login
                cur_u = (target_page.url or "").lower()
                auth_needed = False
                if "myshine/login" in cur_u:
                    if not is_logged_in_to_shine(target_page):
                        auth_needed = True
                elif "job-apply-flow" not in cur_u:
                    try:
                        signin_prompt = target_page.locator("button:has-text('Sign in to Apply'), a:has-text('Sign in to Apply')").first
                        if signin_prompt.count() > 0 and signin_prompt.is_visible(timeout=800):
                            if not is_logged_in_to_shine(target_page):
                                auth_needed = True
                    except Exception:
                        pass

                if auth_needed:
                    logging.error("[Shine] Authentication required. Redirected to login.")
                    print("[Shine] Authentication required. Please run: python3 run_all.py --login-shine")
                    ledger["login_verified"] = False
                    save_shine_ledger(logs_dir, ledger)
                    if is_popup and target_page and target_page != page:
                        try:
                            target_page.close()
                        except Exception:
                            pass
                    return applied_count

                # Check for questionnaire / apply onboarding / modal confirmation
                modal = target_page.locator(
                    "div.modal, div[class*='modal'], div.apply_modal, div[class*='applyDialog'], "
                    "div[class*='job-apply-flow'], div[class*='onboarding']"
                ).first
                if modal.count() > 0 and modal.is_visible(timeout=2000):
                    confirm_btn = modal.locator(
                        "button:has-text('Apply'), button:has-text('Submit'), button:has-text('Continue'), "
                        "button.btn-primary, button:has-text('Next')"
                    ).first
                    if confirm_btn.count() > 0 and confirm_btn.is_visible(timeout=1500):
                        confirm_btn.click(timeout=3000)
                        human_delay(1.5, 2.5)

                if is_popup and target_page and target_page != page:
                    try:
                        target_page.close()
                    except Exception:
                        pass

                if submitted:
                    applied_records[job_id] = {
                        "company": company,
                        "title": title,
                        "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "APPLIED",
                        "channel": "Shine"
                    }
                    applied_count += 1
                    save_shine_ledger(logs_dir, ledger)
                    logging.info(f"[Shine] Successfully applied to '{title}' at '{company}' ({applied_count}/{target_applies})")
                    print(f"[Shine] Successfully applied: '{title}' at '{company}'")

                human_delay(2.5, 4.5)

                human_delay(2.5, 4.5)
            except Exception as e:
                logging.warning(f"[Shine] Error processing card #{idx}: {e}")
                continue

    save_shine_ledger(logs_dir, ledger)
    notify_macos("Shine Auto-Applier", f"Finished run: Applied to {applied_count} jobs today.")
    return applied_count


def run_shine_pipeline(mode="apply", max_applies=None, force_headed=False):
    """Entry point for Shine automation pipeline."""
    config = email_agent.load_config()
    sh_cfg = config.get("shine", {})
    if not sh_cfg.get("enabled", True):
        logging.info("[Shine] Shine agent is disabled in config.json.")
        return

    logs_dir = os.path.expanduser(config.get("paths", {}).get("logs", "~/Documents/JobAutomation/logs"))
    ledger = load_shine_ledger(logs_dir)

    needs_initial_login = not ledger.get("login_verified", False)
    is_headed = force_headed or (mode == "login") or needs_initial_login

    try:
        with sync_playwright() as playwright:
            context = launch_shine_browser(playwright, config, force_headed=is_headed)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.bring_to_front()
            except Exception:
                pass

            try:
                wait_secs = 300 if (mode == "login" or needs_initial_login) else 30
                logged_in = ensure_logged_in(page, interactive_wait_seconds=wait_secs)
                if not logged_in:
                    logging.error("[Shine] Authentication required. Run with --login-shine to authenticate.")
                    print("[Shine] Authentication required. Please run: python3 run_all.py --login-shine")
                    return

                ledger["login_verified"] = True
                save_shine_ledger(logs_dir, ledger)

                if mode == "login":
                    logging.info("[Shine] Login verification complete! Persistent profile is ready.")
                    print("\n[SUCCESS] Shine session verified and persistent Chrome profile is saved!\n")
                    return

                if mode in ("refresh", "all"):
                    refresh_shine_profile(page, config, ledger, logs_dir)

                if mode in ("apply", "all"):
                    apply_to_shine_jobs(page, config, ledger, logs_dir, max_applies=max_applies)

            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[Shine] Execution cancelled by user.")
        return


def main():
    parser = argparse.ArgumentParser(description="Shine Automation Agent")
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
        run_shine_pipeline(mode=args.mode, max_applies=args.max_applies, force_headed=force_headed)


if __name__ == "__main__":
    main()
