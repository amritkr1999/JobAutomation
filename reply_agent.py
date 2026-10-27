#!/usr/bin/env python3
"""
Intelligent Recruiter Email Reply Engine (reply_agent.py)
Generates context-aware, high-conversion email responses to recruiters based on intent,
preserves email threading headers (In-Reply-To / References), supports resume attachments,
and provides Telegram human-in-the-loop approval and autonomous dispatch modes.
"""

import os
import re
import json
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import requests
import crm_tracker
import task_manager
import power_utils

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
DRAFTS_FILE = os.path.join(LOGS_DIR, 'pending_reply_drafts.json')


def load_config() -> dict:
    """Load configuration safely."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"[ReplyAgent] Error loading config: {e}")
    return {}


def load_drafts() -> dict:
    """Load pending email reply drafts."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not os.path.exists(DRAFTS_FILE):
        return {"drafts": {}}
    try:
        with open(DRAFTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"drafts": {}}


def save_drafts(drafts_data: dict) -> bool:
    """Save pending drafts atomically."""
    temp_path = DRAFTS_FILE + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(drafts_data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(temp_path, DRAFTS_FILE)
        return True
    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return False


def query_ollama_draft(prompt: str, config: dict = None) -> str:
    """Attempt to draft reply using local Ollama instance."""
    config = config or load_config()
    host = config.get('ollama', {}).get('host', 'http://localhost:11434')
    model = config.get('ollama', {}).get('model', 'llama3.1:8b')
    url = f"{host.rstrip('/')}/api/generate"

    system_prompt = (
        "You are Amrit Kumar, an experienced Full Stack Software Engineer (3.5 YOE: Java 21, Spring Boot 3, React 19, "
        "FastAPI, AWS). You are replying to an HR/technical recruiter. Write a courteous, professional, concise, "
        "and action-oriented email reply. Do NOT include markdown code blocks, placeholders like [Your Name], or robotic pleasantries. "
        "Sign off as 'Amrit Kumar'."
    )

    try:
        res = requests.post(url, json={
            "model": model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "options": {"temperature": 0.3}
        }, timeout=25)
        if res.status_code == 200:
            return res.json().get('response', '').strip()
    except Exception as e:
        logging.debug(f"[ReplyAgent] Ollama generation fallback to template: {e}")
    return ""


def draft_response_content(intent: str, recruiter_name: str, company: str, role: str,
                           original_subject: str, original_body: str,
                           applied_links_summary: str = "", config: dict = None) -> str:
    """Generate professional, context-rich response text based on recruiter intent."""
    config = config or load_config()
    copilot_cfg = config.get("recruiter_copilot", {})
    profile = config.get("profile", {})

    greeting_name = recruiter_name.strip() if recruiter_name and not recruiter_name.startswith("http") else "Hiring Team"
    company_name = company if company and company not in ("Company", "Unknown Company") else "the team"
    role_name = role if role else "the open position"

    slots = copilot_cfg.get("available_slots", [
        "Monday to Friday, 11:00 AM – 1:00 PM IST",
        "Monday to Friday, 3:00 PM – 6:00 PM IST"
    ])
    slots_formatted = "\n".join([f"• {s}" for s in slots])
    phone = copilot_cfg.get("default_phone", profile.get("phone", "+91-9876543210"))
    notice = copilot_cfg.get("notice_period", profile.get("notice_period", "7 days"))

    # Try Ollama first
    prompt = f"""
Recruiter Name: {greeting_name}
Company: {company_name}
Role: {role_name}
Recruiter Intent: {intent}
Applied Link Proof: {applied_links_summary}
Candidate Details: Notice Period: {notice}, Phone: {phone}, Availability Slots: {slots}
Recruiter Email Snippet:
"{original_body[:300]}"

Draft the exact email reply text. Be direct and polite.
"""
    ai_draft = query_ollama_draft(prompt, config)
    if ai_draft and len(ai_draft) > 60 and "Amrit" in ai_draft:
        return ai_draft

    # Deterministic high-converting template fallbacks
    if intent in ("LINK_PROVIDED", "APPLICATION_LINK_REQUIRED") or applied_links_summary:
        body = (
            f"Hi {greeting_name},\n\n"
            f"Thank you for sharing the application details for the {role_name} role at {company_name}.\n\n"
            f"I have successfully submitted my application via the link provided and attached my updated resume for your records.\n\n"
            f"I am very excited about the work {company_name} is doing and would welcome the opportunity to discuss how my 3.5 years of experience in Java, Spring Boot microservices, and React can add value to your team.\n\n"
            f"Looking forward to hearing from you regarding the next steps.\n\n"
            f"Best regards,\n"
            f"Amrit Kumar\n"
            f"{phone}"
        )

    elif intent in ("SCREENING_REQUEST", "CALL_AVAILABILITY", "INTERVIEW_INVITE"):
        body = (
            f"Hi {greeting_name},\n\n"
            f"Thank you for reaching out! I would be delighted to connect for a preliminary discussion regarding the {role_name} opening at {company_name}.\n\n"
            f"I am available for a call during any of the following windows:\n"
            f"{slots_formatted}\n\n"
            f"You can reach me directly on my cell at {phone}. Alternatively, if there is a calendar invite or specific time that suits you better, please feel free to send it over.\n\n"
            f"Looking forward to speaking with you!\n\n"
            f"Best regards,\n"
            f"Amrit Kumar\n"
            f"{phone}"
        )

    elif intent == "CTC_NOTICE_QUERY":
        body = (
            f"Hi {greeting_name},\n\n"
            f"Thank you for getting back to me regarding the {role_name} role at {company_name}!\n\n"
            f"Here are the requested details for your consideration:\n"
            f"• Notice Period: {notice} (Immediate joiner)\n"
            f"• Total & Relevant Experience: 3.5 Years (Java 21, Spring Boot 3, Microservices, React 19, FastAPI, PostgreSQL, AWS)\n"
            f"• Current Location: Bengaluru (Open to Remote, Hybrid, or Relocation)\n"
            f"• Compensation / CTC: Open for discussion aligned with the position and company standards.\n\n"
            f"I have also attached my latest resume for your review. Please let me know what the upcoming stages in your interview process look like.\n\n"
            f"Best regards,\n"
            f"Amrit Kumar\n"
            f"{phone}"
        )

    elif intent in ("TECH_ASSESSMENT", "ASSESSMENT_TEST"):
        body = (
            f"Hi {greeting_name},\n\n"
            f"Thank you for sharing the assessment details for {company_name}.\n\n"
            f"I have received the challenge and will complete and submit it within the requested timeframe. I will update you once my submission is complete.\n\n"
            f"Best regards,\n"
            f"Amrit Kumar\n"
            f"{phone}"
        )

    elif intent == "RESUME_REQUEST":
        body = (
            f"Hi {greeting_name},\n\n"
            f"Thank you for following up! Please find attached my updated resume detailing my background and recent engineering projects across Java, Spring Boot, and modern cloud stacks.\n\n"
            f"I would be glad to walk you through my experience at your convenience.\n\n"
            f"Best regards,\n"
            f"Amrit Kumar\n"
            f"{phone}"
        )

    elif intent == "REJECTION":
        body = (
            f"Hi {greeting_name},\n\n"
            f"Thank you for letting me know and for taking the time to review my profile for the {role_name} position.\n\n"
            f"I appreciate your team's consideration and would love to stay in touch for any future opportunities at {company_name} that align with my background in Java and backend architecture.\n\n"
            f"Wishing you and the {company_name} team continued success!\n\n"
            f"Best regards,\n"
            f"Amrit Kumar\n"
            f"{phone}"
        )

    else:
        body = (
            f"Hi {greeting_name},\n\n"
            f"Thank you for getting in touch regarding opportunities at {company_name}.\n\n"
            f"I am actively exploring Full Stack and Backend engineering opportunities (Java / Spring Boot / React / Python) and would love to learn more about how I can contribute to your team.\n\n"
            f"Please let me know if you would like to schedule a brief introductory call.\n\n"
            f"Best regards,\n"
            f"Amrit Kumar\n"
            f"{phone}"
        )

    return body.strip()


def send_email_reply(to_email: str, subject: str, body_text: str,
                     in_reply_to: str = "", references: str = "",
                     attach_resume: bool = True, config: dict = None) -> bool:
    """
    Dispatches email via Gmail SMTP using proper RFC 2822 threading headers
    (In-Reply-To and References) and attaches master resume PDF.
    """
    config = config or load_config()
    email_cfg = config.get("email", {})
    sender_email = email_cfg.get("sender_email")
    app_password = email_cfg.get("sender_app_password")
    smtp_server = email_cfg.get("smtp_server", "smtp.gmail.com")
    smtp_port = int(email_cfg.get("smtp_port", 587))
    sender_name = email_cfg.get("sender_name", "Amrit Kumar")

    if not sender_email or not app_password or app_password == "YOUR_GMAIL_APP_PASSWORD":
        logging.error("[ReplyAgent] SMTP credentials missing in config.json.")
        return False

    msg = MIMEMultipart()
    msg['From'] = f"{sender_name} <{sender_email}>"
    msg['To'] = to_email
    clean_sub = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg['Subject'] = clean_sub

    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
    if references or in_reply_to:
        ref_header = f"{references} {in_reply_to}".strip()
        msg['References'] = ref_header

    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))

    # Attach resume PDF if requested
    if attach_resume:
        try:
            import naukri_agent
            resume_path = naukri_agent.ensure_master_resume_pdf(config)
            if resume_path and os.path.exists(resume_path):
                with open(resume_path, 'rb') as f:
                    part = MIMEApplication(f.read(), Name=os.path.basename(resume_path))
                part['Content-Disposition'] = f'attachment; filename="{os.path.basename(resume_path)}"'
                msg.attach(part)
        except Exception as e:
            logging.debug(f"[ReplyAgent] Could not attach resume: {e}")

    try:
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=20)
        server.starttls()
        server.login(sender_email, app_password)
        server.sendmail(sender_email, [to_email], msg.as_string())
        server.quit()
        logging.info(f"[ReplyAgent] Dispatched email reply to {to_email} (Subject: {clean_sub})")
        return True
    except Exception as e:
        logging.error(f"[ReplyAgent] Failed to send reply via SMTP: {e}")
        return False


def queue_or_dispatch_reply(recruiter_email: str, recruiter_name: str, company: str,
                            role: str, original_subject: str, original_body: str,
                            intent: str, in_reply_to: str = "", references: str = "",
                            applied_links_summary: str = "", config: dict = None) -> dict:
    """
    Creates context-aware reply. In Review Mode (default), saves draft and alerts
    via Telegram with inline approval buttons. In Auto mode, dispatches immediately.
    """
    config = config or load_config()
    copilot_cfg = config.get("recruiter_copilot", {})
    review_mode = copilot_cfg.get("review_mode", True)
    auto_reply = copilot_cfg.get("auto_reply_enabled", False)

    draft_body = draft_response_content(
        intent=intent,
        recruiter_name=recruiter_name,
        company=company,
        role=role,
        original_subject=original_subject,
        original_body=original_body,
        applied_links_summary=applied_links_summary,
        config=config
    )

    clean_sub = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
    draft_id = f"DRAFT-{os.urandom(3).hex().upper()}"

    draft_record = {
        "draft_id": draft_id,
        "recruiter_email": recruiter_email,
        "recruiter_name": recruiter_name,
        "company": company,
        "role": role,
        "subject": clean_sub,
        "body": draft_body,
        "in_reply_to": in_reply_to,
        "references": references,
        "intent": intent,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "PENDING"
    }

    # Autonomous Mode Check
    if not review_mode and auto_reply:
        sent = send_email_reply(
            to_email=recruiter_email,
            subject=clean_sub,
            body_text=draft_body,
            in_reply_to=in_reply_to,
            references=references,
            config=config
        )
        if sent:
            draft_record["status"] = "SENT"
            draft_record["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            crm_tracker.update_stage(recruiter_email, "REPLIED", note="Dispatched autonomous email response.")
            power_utils.notify_macos("Replied to Recruiter!", f"Sent auto-reply to {recruiter_email} ({company})")
            return draft_record

    # Otherwise: Review Mode (Queue Draft + Telegram Notification)
    drafts_data = load_drafts()
    drafts_data.setdefault("drafts", {})[draft_id] = draft_record
    save_drafts(drafts_data)

    # Create high-priority review task
    task_manager.create_task(
        title=f"Review & Send Reply Draft to {company or recruiter_name}",
        task_type="REVIEW_REPLY_DRAFT",
        recruiter_email=recruiter_email,
        company=company,
        role=role,
        priority="HIGH",
        description=f"Draft ID: {draft_id}\n\nProposed Reply:\n{draft_body}",
        metadata={"draft_id": draft_id}
    )

    # Dispatch interactive Telegram alert
    try:
        import notifier
        tg = notifier.get_notifier().tg
        if tg.is_configured():
            msg_text = (
                f"✉️ *RECRUITER REPLY DRAFT READY*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 *Company:* `{company}` | 👤 *Recruiter:* `{recruiter_name or recruiter_email}`\n"
                f"🎯 *Intent:* `{intent}`\n\n"
                f"📝 *Proposed Reply Draft:*\n"
                f"```text\n{draft_body}\n```"
            )
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "🚀 Approve & Send", "callback_data": f"approve_reply:{draft_id}"},
                        {"text": "❌ Skip / Dismiss", "callback_data": f"dismiss_reply:{draft_id}"}
                    ]
                ]
            }
            tg.send_message(msg_text, reply_markup=reply_markup)
    except Exception as e:
        logging.debug(f"[ReplyAgent] Telegram alert failed: {e}")

    return draft_record


def approve_and_send_draft(draft_id: str) -> bool:
    """Approve a queued draft and send it via SMTP."""
    drafts_data = load_drafts()
    draft = drafts_data.get("drafts", {}).get(draft_id)
    if not draft:
        logging.error(f"[ReplyAgent] Draft ID {draft_id} not found.")
        return False

    success = send_email_reply(
        to_email=draft["recruiter_email"],
        subject=draft["subject"],
        body_text=draft["body"],
        in_reply_to=draft.get("in_reply_to", ""),
        references=draft.get("references", "")
    )

    if success:
        draft["status"] = "SENT"
        draft["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_drafts(drafts_data)
        crm_tracker.update_stage(draft["recruiter_email"], "REPLIED", note=f"Approved and sent draft {draft_id}.")

        # Mark corresponding review task as completed
        tasks_data = task_manager.load_tasks()
        for tid, t in tasks_data.get("tasks", {}).items():
            if t.get("metadata", {}).get("draft_id") == draft_id:
                task_manager.complete_task(tid, "Sent via Telegram/CLI approval.")

    return success


def cancel_pending_drafts_for_recruiter(recruiter_email: str = "", company: str = "", reason: str = "") -> int:
    """Cancel pending drafts for a recruiter or company when rejected or concluded."""
    drafts_data = load_drafts()
    drafts = drafts_data.get("drafts", {})
    cancelled = 0

    comp_lower = company.strip().lower() if company else ""
    email_lower = recruiter_email.strip().lower() if recruiter_email else ""

    for did, d in drafts.items():
        if d.get("status") != "PENDING":
            continue
        d_comp = d.get("company", "").lower()
        d_email = d.get("recruiter_email", "").lower()

        if (email_lower and email_lower == d_email) or (comp_lower and (comp_lower in d_comp or d_comp in comp_lower)):
            d["status"] = "CANCELLED"
            d["cancellation_reason"] = reason or "Opportunity concluded / rejected."
            cancelled += 1
            logging.info(f"[ReplyAgent] Cancelled stale draft {did} for {d_comp} ({d_email})")

    if cancelled > 0:
        save_drafts(drafts_data)

    return cancelled


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--approve":
        did = sys.argv[2] if len(sys.argv) > 2 else ""
        if did:
            res = approve_and_send_draft(did)
            print(f"Draft {did} send status: {'SUCCESS' if res else 'FAILED'}")
        else:
            print("Usage: python3 reply_agent.py --approve <DRAFT_ID>")
    else:
        drafts = load_drafts().get("drafts", {})
        pending = [d for d in drafts.values() if d.get("status") == "PENDING"]
        print(f"\n📋 PENDING REPLY DRAFTS: {len(pending)}")
        for d in pending:
            print(f"• [{d['draft_id']}] {d['company']} ({d['recruiter_email']}) - Intent: {d['intent']}")
