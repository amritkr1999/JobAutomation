#!/usr/bin/env python3
"""
Autonomous Recruiter Link Application Engine (recruiter_link_applier.py)
Processes links sent by recruiters (Google Forms, MS Forms, Greenhouse, Workday, etc.),
autonomously solves the forms, captures submission proof, updates the CRM,
and feeds confirmation context into the email response drafter.
"""

import os
import re
import json
import logging
import argparse
from datetime import datetime

import ats_bridge
import crm_tracker
import task_manager
import power_utils

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')


def load_config() -> dict:
    """Load configuration safely."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"[LinkApplier] Config load error: {e}")
    return {}


def apply_to_recruiter_link(url: str, recruiter_email: str = "", company: str = "",
                           role: str = "", dry_run: bool = False, force_headed: bool = False,
                           review_mode: bool = False) -> dict:
    """
    Intelligently unshortens, classifies, and executes an application form/link
    sent by a recruiter. Updates the CRM ledger and records proof.
    """
    config = load_config()
    clean_url = ats_bridge.clean_url(url)
    logging.info(f"[LinkApplier] Evaluating recruiter link: {clean_url}")

    # 1. Unshorten URL to discover canonical destination
    resolved_url = ats_bridge.unshorten_url(clean_url, timeout=4.0)
    target_url = resolved_url or clean_url

    # 2. Classify ATS Type
    ats_type, direct_url = ats_bridge.detect_direct_ats_url(target_url)
    if not ats_type:
        ats_type, direct_url = ats_bridge.find_ats_url_in_text(target_url)

    final_url = direct_url or target_url
    logging.info(f"[LinkApplier] Classified as: {ats_type or 'generic'} -> {final_url}")

    result = {
        "url": final_url,
        "ats_type": ats_type or "generic_portal",
        "company": company or "Hiring Company",
        "role": role or "Software Engineer",
        "recruiter_email": recruiter_email,
        "success": False,
        "screenshot": "",
        "notes": ""
    }

    # 3. Autonomous Execution via ATS Engines
    if ats_type in ("google_forms", "ms_forms", "form", "forms", "google_form", "ms_form"):
        import forms_agent
        try:
            logging.info(f"[LinkApplier] Launching Forms Agent for {final_url}...")
            success = forms_agent.apply_single_url(
                final_url, config, dry_run=dry_run, force_headed=force_headed, review_mode=review_mode
            )
            result["success"] = bool(success)
            result["notes"] = "Submitted via Forms Agent (Google/Microsoft Forms Solver)."
        except Exception as e:
            logging.error(f"[LinkApplier] Forms Agent error: {e}")
            result["notes"] = f"Error during form solving: {e}"

    elif ats_type == "greenhouse":
        import greenhouse_agent
        try:
            logging.info(f"[LinkApplier] Launching Greenhouse Agent for {final_url}...")
            success = greenhouse_agent.apply_single_url(
                final_url, config, dry_run=dry_run, force_headed=force_headed, review_mode=review_mode
            )
            result["success"] = bool(success)
            result["notes"] = "Submitted via Greenhouse ATS Agent."
        except Exception as e:
            logging.error(f"[LinkApplier] Greenhouse Agent error: {e}")
            result["notes"] = f"Error during Greenhouse solving: {e}"

    elif ats_type == "workday":
        import workday_agent
        try:
            logging.info(f"[LinkApplier] Launching Workday Agent for {final_url}...")
            success = workday_agent.apply_single_url(
                final_url, config, dry_run=dry_run, force_headed=force_headed, review_mode=review_mode
            )
            result["success"] = bool(success)
            result["notes"] = "Submitted via Workday ATS Agent."
        except Exception as e:
            logging.error(f"[LinkApplier] Workday Agent error: {e}")
            result["notes"] = f"Error during Workday solving: {e}"

    else:
        # Generic portal / unsupported ATS: Queue high-priority manual task
        logging.info(f"[LinkApplier] Unsupported direct ATS for {final_url}. Queuing candidate action task.")
        t = task_manager.create_task(
            title=f"Apply to Portal Link for {company or 'Recruiter'}",
            task_type="APPLY_LINK_MANUAL",
            recruiter_email=recruiter_email,
            company=company,
            role=role,
            priority="HIGH",
            description=f"Recruiter requested application submission at external portal:\n{final_url}",
            action_url=final_url
        )
        result["notes"] = f"Queued high-priority manual task: {t.get('id')}"

    # 4. Update CRM Ledger
    if recruiter_email:
        crm_tracker.record_link_application(
            email=recruiter_email,
            url=final_url,
            ats_type=result["ats_type"],
            success=result["success"],
            screenshot=result["screenshot"],
            notes=result["notes"]
        )

    # 5. Notify macOS Banner if executed
    if result["success"]:
        power_utils.notify_macos(
            f"Auto-Applied to Recruiter Link!",
            f"{company}: Successfully applied via {result['ats_type'].upper()}.",
            is_urgent=True
        )

    return result


def batch_apply_pending_recruiter_links(dry_run: bool = False, force_headed: bool = False) -> list:
    """Scan CRM for pending recruiter links and attempt autonomous application."""
    crm = crm_tracker.load_crm()
    results = []

    for norm_email, thread in crm.get("threads", {}).items():
        extracted = thread.get("extracted_links", [])
        for link_obj in extracted:
            if link_obj.get("status") == "PENDING":
                url = link_obj.get("url")
                company = thread.get("company", "")
                role = thread.get("role", "")
                res = apply_to_recruiter_link(
                    url=url,
                    recruiter_email=norm_email,
                    company=company,
                    role=role,
                    dry_run=dry_run,
                    force_headed=force_headed
                )
                results.append(res)

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recruiter Link Autonomous Applier")
    parser.add_argument("--url", help="Direct URL to apply to")
    parser.add_argument("--company", default="Hiring Company", help="Company Name")
    parser.add_argument("--role", default="Full Stack Developer", help="Role Title")
    parser.add_argument("--email", default="", help="Recruiter Email Address")
    parser.add_argument("--dry-run", action="store_true", help="Preview form without submitting")
    parser.add_argument("--headed", action="store_true", help="Launch visible browser window")
    parser.add_argument("--batch", action="store_true", help="Process all pending CRM links")

    args = parser.parse_args()

    if args.url:
        res = apply_to_recruiter_link(
            url=args.url,
            recruiter_email=args.email,
            company=args.company,
            role=args.role,
            dry_run=args.dry_run,
            force_headed=args.headed
        )
        print("\n" + "=" * 60)
        print(f"🎯 Execution Result: {'SUCCESS' if res['success'] else 'NEEDS MANUAL / PENDING'}")
        print(f"Notes: {res['notes']}")
        print("=" * 60)
    elif args.batch:
        batch_res = batch_apply_pending_recruiter_links(dry_run=args.dry_run, force_headed=args.headed)
        print(f"Processed {len(batch_res)} pending links.")
    else:
        parser.print_help()
