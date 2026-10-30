#!/usr/bin/env python3
"""
Recruiter Copilot Unified Orchestration Engine (recruiter_copilot.py)
Orchestrates the entire closed-loop inbound recruiter pipeline:
  1. Ingests recruiter emails via IMAP (recruiter_harvester.py)
  2. Updates CRM relationship state machine (crm_tracker.py)
  3. Extracts & auto-applies to recruiter links (recruiter_link_applier.py)
  4. Creates candidate action items & reminders (task_manager.py)
  5. Drafts context-aware email replies with thread headers & PDF attachments (reply_agent.py)
  6. Dispatches interactive Telegram notifications with one-tap action buttons.
"""

import os
import sys
import json
import logging
from datetime import datetime

import crm_tracker
import task_manager
import recruiter_harvester
import recruiter_link_applier
import reply_agent
import power_utils

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')


def load_config() -> dict:
    """Load configuration safely."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"[RecruiterCopilot] Config load error: {e}")
    return {}


def run_recruiter_copilot_cycle(lookback_days: int = 14, max_emails: int = 50,
                                dry_run: bool = False, force_headed: bool = False) -> dict:
    """
    Executes a complete, unified recruiter communication cycle.
    """
    config = load_config()
    copilot_cfg = config.get("recruiter_copilot", {})
    if not copilot_cfg.get("enabled", True):
        logging.info("[RecruiterCopilot] Feature is disabled in config.json.")
        return {"status": "DISABLED"}

    logging.info("=" * 70)
    logging.info("🤖 STARTING RECRUITER COPILOT INBOUND SWEEP")
    logging.info("=" * 70)

    # 1. Harvest & classify new recruiter messages
    harvest_res = recruiter_harvester.harvest_recruiter_emails(
        lookback_days=lookback_days,
        max_emails=max_emails
    )

    if harvest_res.get("status") != "SUCCESS":
        logging.warning(f"[RecruiterCopilot] Harvester ended with status: {harvest_res.get('status')}")
        return harvest_res

    leads = harvest_res.get("leads", [])
    links_to_apply = harvest_res.get("links_to_apply", [])

    logging.info(f"[RecruiterCopilot] Ingested {len(leads)} active recruiter leads | {len(links_to_apply)} links to process.")

    applied_results = []
    # 2. Process application links if auto_apply is enabled
    if links_to_apply and copilot_cfg.get("auto_apply_links", True):
        for item in links_to_apply:
            res = recruiter_link_applier.apply_to_recruiter_link(
                url=item["url"],
                recruiter_email=item["recruiter_email"],
                company=item["company"],
                role=item["role"],
                dry_run=dry_run,
                force_headed=force_headed
            )
            applied_results.append(res)

    # 3. Generate context-aware replies for actionable leads only
    drafts_created = []
    for lead in leads:
        r_email = lead["from_email"]
        r_name = lead.get("recruiter_name", "")
        company = lead.get("company", "Company")
        role = lead.get("role", "Software Engineer")
        subject = lead.get("subject", "")
        body_text = lead.get("body", "")
        intent = lead.get("intent", "GENERAL_INQUIRY")
        message_id = lead.get("message_id", "")

        # Skip non-actionable intents or ignored broadcasts
        if intent in ("IGNORE", "APPLICATION_RECEIPT", "REJECTION", "INTERVIEW_COMPLETED"):
            continue

        # Check if application link was completed for this recruiter
        link_proof = ""
        matching_applied = [a for a in applied_results if a.get("recruiter_email") == r_email and a.get("success")]
        if matching_applied:
            link_proof = f"Successfully submitted application via {matching_applied[0]['ats_type'].upper()} at {matching_applied[0]['url']}"

        draft_rec = reply_agent.queue_or_dispatch_reply(
            recruiter_email=r_email,
            recruiter_name=r_name,
            company=company,
            role=role,
            original_subject=subject,
            original_body=body_text,
            intent=intent,
            in_reply_to=message_id,
            applied_links_summary=link_proof,
            config=config
        )
        if draft_rec:
            drafts_created.append(draft_rec)

    # 4. Consolidated reporting
    summary = {
        "status": "COMPLETED",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "new_messages_ingested": len(leads),
        "links_processed": len(applied_results),
        "links_successful": len([a for a in applied_results if a.get("success")]),
        "reply_drafts_prepared": len(drafts_created),
        "active_pending_tasks": len(task_manager.get_pending_tasks())
    }

    logging.info(f"[RecruiterCopilot] Cycle Summary: {json.dumps(summary, indent=2)}")
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    dry = "--dry-run" in sys.argv
    res = run_recruiter_copilot_cycle(dry_run=dry)
    print("\n" + "=" * 60)
    print(f"🎉 RECRUITER COPILOT RUN COMPLETED: {res.get('status')}")
    print(f"• Inbound Messages: {res.get('new_messages_ingested', 0)}")
    print(f"• Links Processed:  {res.get('links_processed', 0)}")
    print(f"• Drafts Prepared:  {res.get('reply_drafts_prepared', 0)}")
    print("=" * 60)
