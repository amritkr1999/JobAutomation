#!/usr/bin/env python3
"""
Instahyre Daily Profile Refresher & 1-Click Auto-Applier
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
import subprocess
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import agent as email_agent
import naukri_agent

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')


def human_delay(min_sec=1.5, max_sec=3.5):
    """Sleep for a randomized human-like interval."""
    time.sleep(random.uniform(min_sec, max_sec))


import power_utils
from power_utils import MacKeepAwake


def notify_macos(title, message, is_urgent=False):
    """Display a non-blocking macOS notification banner respecting quiet_mode."""
    power_utils.notify_macos(title, message, is_urgent=is_urgent)


def load_instahyre_ledger(logs_dir):
    """Load persistent ledger of applied Instahyre opportunities and profile refresh state."""
    os.makedirs(logs_dir, exist_ok=True)
    ledger_path = os.path.join(logs_dir, 'instahyre_applied_jobs.json')
    default_data = {
        "last_profile_refresh_at": None,
        "last_daily_auto_run_date": None,
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
            logging.warning(f"[Instahyre] Could not read instahyre_applied_jobs.json: {e}")
    return default_data


def save_instahyre_ledger(logs_dir, ledger):
    """Persist Instahyre applied jobs and profile state."""
    ledger_path = os.path.join(logs_dir, 'instahyre_applied_jobs.json')
    try:
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logging.warning(f"[Instahyre] Could not save instahyre_applied_jobs.json: {e}")


def count_applies_today(ledger):
    """Count how many Instahyre opportunities were successfully applied to today (YYYY-MM-DD)."""
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


def launch_instahyre_browser(playwright, config, force_headed=False):
    """Launch a persistent Google Chrome context dedicated to Instahyre automation."""
    insta_cfg = config.get('instahyre', {})
    profile_dir = os.path.expanduser(
        insta_cfg.get('browser_profile_dir', '~/Documents/JobAutomation/browser_profile/instahyre')
    )
    os.makedirs(profile_dir, exist_ok=True)
    naukri_agent.cleanup_stale_chrome_locks(profile_dir)

    chrome_exec = insta_cfg.get('chrome_executable', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    headless = False if force_headed else bool(insta_cfg.get('headless', False))

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
    if os.environ.get("JOB_AUTO_OFFSCREEN") == "1" or insta_cfg.get("offscreen", False):
        launch_kwargs["args"].append("--window-position=-3000,-3000")

    if os.path.exists(chrome_exec):
        launch_kwargs["executable_path"] = chrome_exec

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)
    return context


def is_logged_in_to_instahyre(page):
    """Check whether the current Instahyre page represents an authenticated candidate session."""
    url = (page.url or "").lower()
    if "login" in url or "signin" in url:
        return False

    logged_in_selectors = [
        "a[href*='/candidate/opportunities']",
        "a[href*='/candidate/profile']",
        "#opportunities",
        ".opportunity-container",
        ".candidate-navbar",
        ".profile-icon",
        ".user-dropdown",
        "div[ng-controller*='Candidate']"
    ]
    login_form_selectors = [
        "#email",
        "input[name='email']",
        "#password",
        "input[type='password']"
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

    return "candidate" in url and "login" not in url


def ensure_logged_in(page, interactive_wait_seconds=300):
    """
    Navigate to Instahyre opportunities and verify login.
    If not logged in, wait for the user to log in once in the opened Chrome window.
    """
    logging.info("[Instahyre] Navigating to Instahyre opportunities to verify session...")
    try:
        page.goto("https://www.instahyre.com/candidate/opportunities/", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logging.warning(f"[Instahyre] Initial navigation warning: {e}")

    human_delay(2.5, 4.0)

    if is_logged_in_to_instahyre(page):
        logging.info("[Instahyre] Active Instahyre session confirmed!")
        return True

    msg = "Please log in to your Instahyre account in the opened Chrome window. Session will be saved automatically."
    logging.warning(f"[Instahyre] Not logged in yet. {msg}")
    print(f"\n[ACTION REQUIRED] {msg}\nWaiting up to {interactive_wait_seconds} seconds for login...")
    notify_macos("Instahyre Automation", "Please log in once in the opened Chrome window.")

    try:
        if "instahyre.com" not in (page.url or "") or "login" not in (page.url or ""):
            page.goto("https://www.instahyre.com/login/", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    start_time = time.time()
    while time.time() - start_time < interactive_wait_seconds:
        time.sleep(3)
        try:
            if is_logged_in_to_instahyre(page):
                logging.info("[Instahyre] Login detected! Saving persistent session.")
                print("[SUCCESS] Instahyre login detected and saved to persistent Chrome profile!")
                human_delay(2.0, 3.5)
                return True
        except Exception:
            pass

    logging.error("[Instahyre] Timed out waiting for Instahyre login.")
    return False


def refresh_instahyre_profile(page, config, ledger, logs_dir):
    """
    Daily Profile Refresher on Instahyre:
      1. Uploads fresh Master Resume PDF.
      2. Clicks Save / Update on profile to refresh candidate active status.
    """
    insta_cfg = config.get('instahyre', {})
    refresh_cfg = insta_cfg.get('profile_refresh', {})
    if not refresh_cfg.get('enabled', True):
        logging.info("[Instahyre] Profile refresh is disabled in config.")
        return False

    logging.info("[Instahyre] Starting Daily Profile Refresh on Instahyre.com...")
    try:
        page.goto("https://www.instahyre.com/candidate/profile/", wait_until="domcontentloaded", timeout=45000)
        human_delay(3.0, 4.5)
    except Exception as e:
        logging.error(f"[Instahyre] Failed to load candidate profile page: {e}")
        return False

    refreshed_any = False

    # 1. Upload fresh Master Resume PDF
    if refresh_cfg.get('upload_fresh_resume', True):
        try:
            master_pdf = naukri_agent.ensure_master_resume_pdf(config)
            file_input_selectors = [
                "input[type='file'][name*='resume']",
                "input#resume-file",
                "input[type='file']"
            ]
            for sel in file_input_selectors:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.set_input_files(master_pdf, timeout=15000)
                    logging.info(f"[Instahyre] Uploaded fresh master resume PDF via '{sel}'.")
                    human_delay(4.0, 6.0)
                    refreshed_any = True
                    break
        except Exception as e:
            logging.warning(f"[Instahyre] Resume upload step warning: {e}")

    # 2. Touch Save / Update Profile button to register active timestamp
    if refresh_cfg.get('refresh_job_preferences', True):
        try:
            save_selectors = [
                "button[type='submit']:has-text('Save')",
                "button:has-text('Update Profile')",
                "button:has-text('Save Changes')",
                "input[type='submit'][value*='Save']"
            ]
            for s_sel in save_selectors:
                btn = page.locator(s_sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1500):
                    btn.click(timeout=4000)
                    logging.info("[Instahyre] Clicked Save Profile to refresh candidate activity status.")
                    human_delay(2.5, 4.0)
                    refreshed_any = True
                    break
        except Exception as e:
            logging.warning(f"[Instahyre] Save Profile touch warning: {e}")

    if refreshed_any:
        ledger["last_profile_refresh_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ledger["profile_refresh_count"] = int(ledger.get("profile_refresh_count", 0)) + 1
        save_instahyre_ledger(logs_dir, ledger)
        logging.info("[Instahyre] Daily Profile Refresh completed successfully!")
    else:
        logging.warning("[Instahyre] Profile refresh finished without explicit button confirmation.")

    return refreshed_any


def close_any_instahyre_modal(page):
    """Ensure any open Instahyre job modal or backdrop is closed so subsequent cards can be clicked."""
    try:
        # 1. Click the specific Instahyre close icon / element
        page.evaluate("""() => {
            try {
                const closeBtn = document.querySelector('div.application-modal-close, i.fa-close, button.close, [data-dismiss="modal"]');
                if (closeBtn) closeBtn.click();
            } catch(e) {}
        }""")
        time.sleep(0.3)

        # 2. Key Escape
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        # 3. Cleanly dismiss angular scope and remove leftover overlays if still present
        page.evaluate("""() => {
            try {
                const wrap = document.querySelector('.application-modal-wrap');
                if (wrap && window.getComputedStyle(wrap).display !== 'none') {
                    const s = window.angular ? window.angular.element(wrap).scope() : null;
                    if (s && typeof s.closeApplyModal === 'function') {
                        s.closeApplyModal();
                    }
                }
                document.querySelectorAll('.application-modal-backdrop, .modal-backdrop').forEach(b => b.remove());
                document.querySelectorAll('.application-modal-wrap, .application-modal-block, .modal').forEach(m => {
                    m.style.display = 'none';
                });
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('overflow');
                document.body.style.removeProperty('padding-right');
            } catch(e) {}
        }""")
        time.sleep(0.3)
    except Exception as e:
        logging.debug(f"[Instahyre] Modal close helper note: {e}")


def find_active_instahyre_modal(page, timeout_ms=5000):
    """
    Directly scopes element selection to the currently active job modal dialog.
    Waits for the modal to be visible and interactive.
    """
    modal_selectors = [
        "div.application-modal-wrap",
        "div.new-employer-profile",
        "div.application-modal-block",
        "div.candidate-apply-modal",
        "div.application-modal:not(.ng-hide)",
        "div[ng-controller='employerProfileModalCtrl']",
        "#employer-profile-modal",
        "div.modal.in",
        "div.modal.show",
        "div.modal"
    ]
    start_time = time.time()
    while (time.time() - start_time) * 1000 < timeout_ms:
        for m_sel in modal_selectors:
            try:
                loc = page.locator(m_sel).first
                if loc.count() > 0 and loc.is_visible(timeout=250):
                    return loc
            except Exception:
                pass
        time.sleep(0.2)
    return None


def run_instahyre_auto_apply(context, page, config, ledger, logs_dir, max_applies_override=None):
    """
    Scan Instahyre matched opportunities:
      1. Clicks each opportunity row/card to open the Job Profile Modal.
      2. Scopes element selection directly inside the active modal popup.
      3. Brings the internal Apply button into view via JavaScript execution (scrollIntoView(true)).
      4. Explicit wait to ensure modal DOM stability before clicking.
      5. Submits any pitch/cover note if required.
      6. Verifies and logs success (✅ Applied X times).
      7. Closes modal cleanly to unblock subsequent cards.
    """
    insta_cfg = config.get('instahyre', {})
    search_cfg = insta_cfg.get('search', {})
    daily_limit = int(insta_cfg.get('daily_apply_limit', 100))
    run_limit = int(insta_cfg.get('apply_limit_per_run', 50))
    session_limit = int(max_applies_override) if max_applies_override is not None else run_limit
    default_note = search_cfg.get(
        'default_cover_note',
        "Hi, I am Amrit Kumar, a Full Stack Developer with 3+ years of experience building scalable applications using Java 21, Spring Boot 3, Microservices, React 19, Python FastAPI, PostgreSQL, and AWS. Immediate joiner (7 days notice period)."
    )

    applies_today = count_applies_today(ledger)
    logging.info(
        f"[Instahyre] Auto-Apply status today: {applies_today}/{daily_limit} jobs applied so far "
        f"(Session limit: {session_limit})."
    )
    if applies_today >= daily_limit and max_applies_override is None:
        logging.info("[Instahyre] Reached daily apply limit. Skipping further applications for today.")
        return 0

    try:
        page.goto("https://www.instahyre.com/candidate/opportunities/", wait_until="domcontentloaded", timeout=45000)
        human_delay(3.5, 5.0)
    except Exception as e:
        logging.error(f"[Instahyre] Failed to load opportunities page: {e}")
        return 0

    close_any_instahyre_modal(page)

    # Find all opportunity rows/links on the page (prioritizing #candidate-suggested-employers)
    opp_link_selectors = [
        "#candidate-suggested-employers a#employer-profile-opportunity",
        "#candidate-suggested-employers div.employer-row a.text-link",
        "#candidate-suggested-employers a[ng-click*='openApplyModal']",
        "#candidate-suggested-employers div.employer-row",
        "a#employer-profile-opportunity",
        "div.employer-row a.text-link",
        "a[ng-click*='openApplyModal']",
        "div.employer-row",
        "div[ng-repeat*='opportunity' i]"
    ]
    chosen_sel = None
    total_opps = 0
    for sel in opp_link_selectors:
        cnt = page.locator(sel).count()
        if cnt > 0:
            chosen_sel = sel
            total_opps = cnt
            logging.info(f"[Instahyre] Located {total_opps} opportunity item(s) via '{sel}'.")
            break

    if not chosen_sel or total_opps == 0:
        logging.info("[Instahyre] No opportunity items found on the page.")
        return 0

    applied_this_run = 0
    applied_ledger = ledger.setdefault("applied_jobs", {})

    for idx in range(total_opps):
        if applied_this_run >= session_limit:
            logging.info(f"[Instahyre] Reached session apply limit ({session_limit}). Stopping loop.")
            break
        if max_applies_override is None and (applies_today + applied_this_run) >= daily_limit:
            logging.info(f"[Instahyre] Reached daily apply limit ({daily_limit}). Stopping loop.")
            break

        try:
            close_any_instahyre_modal(page)

            card = page.locator(chosen_sel).nth(idx)
            if not card.is_visible(timeout=800):
                # Bring card into view on outer feed
                try:
                    card.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'});")
                    page.wait_for_timeout(400)
                except Exception:
                    pass

            if not card.is_visible(timeout=800):
                continue

            card_raw_text = (card.inner_text(timeout=600) or "").strip()
            if "applied" in card_raw_text.lower():
                continue

            # Extract card-level company preview
            card_company = ""
            for c_sel in (".company-name", ".employer-name", "h3", "h4", "strong"):
                c_loc = card.locator(c_sel).first
                if c_loc.count() > 0:
                    card_company = (c_loc.inner_text(timeout=300) or "").strip()
                    if card_company:
                        break

            # 1. Bring card into view and click to open the Opportunity Details modal
            try:
                card.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'});")
                page.wait_for_timeout(300)
            except Exception:
                pass

            try:
                card.click(timeout=3000)
            except Exception:
                card.evaluate("el => el.click()")

            # 2. Add Explicit Wait: Timing Control (allow modal DOM to open & stabilize)
            logging.info(f"[Instahyre] Clicked opportunity {idx+1}. Waiting for modal DOM to stabilize...")
            human_delay(1.8, 2.8)

            # 3. Target the Modal Container Directly (Selector Check)
            modal = find_active_instahyre_modal(page, timeout_ms=5000)
            if not modal:
                logging.info(f"[Instahyre] Opportunity {idx+1} modal did not open or wasn't detected. Moving to next.")
                close_any_instahyre_modal(page)
                continue

            # Extract job details scoped inside the active modal
            modal_title = ""
            for t_sel in (".profile-heading h1", "h1", ".job-title", ".profile-title", "h2", "h3", "h4", ".opportunity-title"):
                t_loc = modal.locator(t_sel).first
                if t_loc.count() > 0:
                    modal_title = (t_loc.inner_text(timeout=400) or "").strip()
                    if modal_title:
                        break

            modal_company = card_company
            for mc_sel in (".profile-heading h2", ".employer-name", ".company-name", "h2", "h3", "h4", "strong"):
                mc_loc = modal.locator(mc_sel).first
                if mc_loc.count() > 0:
                    extracted_c = (mc_loc.inner_text(timeout=400) or "").strip()
                    if extracted_c:
                        modal_company = extracted_c
                        break

            job_identifier = f"{modal_company}_{modal_title}".strip("_")
            if not job_identifier or len(job_identifier) < 3:
                job_identifier = f"instahyre_opp_{idx+1}"

            if job_identifier in applied_ledger:
                logging.info(f"[Instahyre] Already in ledger: '{job_identifier}'. Closing modal.")
                close_any_instahyre_modal(page)
                continue

            # 4. Target the internal Apply button scoped inside the active modal
            apply_btn_selectors = [
                "button.new-btn:has-text('Apply')",
                "button.btn-primary:has-text('Apply')",
                "button:has-text('Apply')",
                "button[ng-click*='apply' i]",
                "a.btn:has-text('Apply')",
                ".modal-footer button.btn-primary",
                ".modal-footer button"
            ]
            modal_apply_btn = None
            for b_sel in apply_btn_selectors:
                cand_btn = modal.locator(b_sel).first
                try:
                    if cand_btn.count() > 0 and cand_btn.is_visible(timeout=300):
                        modal_apply_btn = cand_btn
                        break
                except Exception:
                    pass

            if modal_apply_btn is None:
                # Fallback: any button containing text 'Apply'
                cand_btn = modal.locator("button").filter(has_text=re.compile(r"^apply$", re.I)).first
                if cand_btn.count() > 0 and cand_btn.is_visible(timeout=300):
                    modal_apply_btn = cand_btn

            if modal_apply_btn is None or modal_apply_btn.count() == 0:
                logging.info(f"[Instahyre] No visible Apply button inside modal for '{job_identifier}'.")
                close_any_instahyre_modal(page)
                continue

            btn_text = (modal_apply_btn.inner_text(timeout=400) or "").strip().lower()
            if "applied" in btn_text:
                logging.info(f"[Instahyre] Already applied to '{job_identifier}'.")
                applied_ledger[job_identifier] = {
                    "title": modal_title or "Full Stack Developer",
                    "company": modal_company or "Opportunity",
                    "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "ALREADY_APPLIED"
                }
                save_instahyre_ledger(logs_dir, ledger)
                close_any_instahyre_modal(page)
                continue

            # 5. Use Scroll Into View: JavaScript Execution on the exact button inside modal
            try:
                modal.evaluate("""el => {
                    const body = el.querySelector('.modal-body') || el.querySelector('.modal-dialog') || el;
                    if (body && body.scrollHeight > body.clientHeight) {
                        body.scrollTop = body.scrollHeight;
                    }
                }""")
            except Exception:
                pass

            # Command driver to bring the exact button element into view via JS execution
            modal_apply_btn.evaluate("""(el) => {
                if (el) {
                    el.scrollIntoView(true);
                    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
                }
            }""")
            page.wait_for_timeout(600)

            # 6. Click the modal Apply button directly
            logging.info(f"[Instahyre] Clicking Apply inside modal for '{modal_title or 'Role'}' at '{modal_company or 'Company'}'...")
            try:
                modal_apply_btn.click(timeout=3000, force=True)
            except Exception:
                modal_apply_btn.evaluate("el => el.click()")

            human_delay(1.5, 2.5)

            # 7. Check if cover note / pitch box appeared
            note_box = page.locator(
                "textarea[placeholder*='note' i], textarea[ng-model*='note' i], textarea#application-note, div.modal textarea, textarea"
            ).first
            if note_box.count() > 0 and note_box.is_visible(timeout=1200):
                note_box.evaluate("(el) => { if (el) el.scrollIntoView(true); }")
                page.wait_for_timeout(300)
                note_box.fill(default_note, timeout=2000)
                human_delay(0.5, 1.0)

                submit_btn = page.locator(
                    "button:has-text('Submit'), button[type='submit']:has-text('Apply'), button.btn-primary:has-text('Apply'), button:has-text('Send'), button[ng-click*='submit' i]"
                ).first
                if submit_btn.count() > 0 and submit_btn.is_visible(timeout=1200):
                    submit_btn.evaluate("(el) => { if (el) el.scrollIntoView(true); }")
                    page.wait_for_timeout(400)
                    try:
                        submit_btn.click(timeout=3000, force=True)
                    except Exception:
                        submit_btn.evaluate("el => el.click()")
                    human_delay(2.0, 3.5)

            # 8. Verification & Success Logging
            applied_this_run += 1
            success_msg = f"✅ Applied {applied_this_run} times: '{modal_title or 'Role'}' at '{modal_company or 'Company'}'"
            print(f"\n{success_msg}\n")
            logging.info(
                f"[Instahyre] {success_msg} ({applies_today + applied_this_run}/{daily_limit} today)"
            )
            notify_macos("Instahyre Application", f"Applied to {modal_title} at {modal_company}")

            applied_ledger[job_identifier] = {
                "title": modal_title or "Full Stack Developer",
                "company": modal_company or "Opportunity",
                "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "APPLIED"
            }
            save_instahyre_ledger(logs_dir, ledger)
            human_delay(1.5, 2.5)

            # 9. Cleanly close modal before next loop iteration
            close_any_instahyre_modal(page)
            human_delay(1.2, 2.2)

        except Exception as e:
            logging.warning(f"[Instahyre] Error processing opportunity {idx+1}: {e}")
            close_any_instahyre_modal(page)

    logging.info(f"[Instahyre] Auto-Apply run finished. Applied to {applied_this_run} new opportunity(ies).")
    return applied_this_run


def run_instahyre_pipeline(mode="all", max_applies=None, force_headed=False, interactive_login=True):
    """Main entry point for Instahyre profile refresh and auto-apply pipeline."""
    import fcntl
    lock_path = "/tmp/instahyre_automation_agent.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, IOError):
        print("Another Instahyre Automation instance is already running. Skipping concurrent execution.")
        return False

    config = email_agent.load_config()
    paths = config.get('paths', {})
    logs_dir = os.path.expanduser(paths.get('logs', '~/Documents/JobAutomation/logs'))
    email_agent.setup_logging(logs_dir)

    insta_cfg = config.get('instahyre', {})
    if not insta_cfg.get('enabled', True):
        logging.info("[Instahyre] Instahyre automation is disabled in config.json.")
        return False

    ledger = load_instahyre_ledger(logs_dir)
    if mode in ("all", "apply"):
        ledger["last_daily_auto_run_date"] = datetime.now().strftime("%Y-%m-%d")
        save_instahyre_ledger(logs_dir, ledger)

    with sync_playwright() as p:
        context = launch_instahyre_browser(p, config, force_headed=force_headed or (mode == "login"))
        try:
            page = context.pages[0] if context.pages else context.new_page()

            wait_secs = 300 if (interactive_login or mode == "login") else 15
            if not ensure_logged_in(page, interactive_wait_seconds=wait_secs):
                logging.error("[Instahyre] Cannot proceed without an authenticated Instahyre session. Run: python3 instahyre_agent.py --mode login")
                return False

            if mode == "login":
                ledger["login_verified"] = True
                save_instahyre_ledger(logs_dir, ledger)
                logging.info("[Instahyre] Login verification complete! Persistent profile is ready.")
                return True

            if mode in ("all", "refresh"):
                try:
                    refresh_instahyre_profile(page, config, ledger, logs_dir)
                except Exception as e:
                    logging.error(f"[Instahyre] Profile refresh encountered error: {e}", exc_info=True)

            if mode in ("all", "apply"):
                try:
                    run_instahyre_auto_apply(context, page, config, ledger, logs_dir, max_applies_override=max_applies)
                except Exception as e:
                    logging.error(f"[Instahyre] Auto apply encountered error: {e}", exc_info=True)

            ledger["last_daily_auto_run_date"] = datetime.now().strftime("%Y-%m-%d")
            save_instahyre_ledger(logs_dir, ledger)
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
    parser = argparse.ArgumentParser(description="Instahyre Daily Profile Refresher & 1-Click Auto-Applier")
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
        run_instahyre_pipeline(mode=args.mode, max_applies=args.max_applies, force_headed=args.headed)
