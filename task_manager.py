#!/usr/bin/env python3
"""
Intelligent Recruiter Task Manager (task_manager.py)
Captures, tracks, prioritizes, and resolves action items originating from recruiter
interactions, interviews, code challenges, document requests, and follow-ups.
"""

import os
import json
import logging
import subprocess
from datetime import datetime, timedelta

BASE_DIR = os.path.expanduser('~/Documents/JobAutomation')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
TASKS_FILE = os.path.join(LOGS_DIR, 'tasks.json')

TASK_TYPES = [
    "SCHEDULE_CALL",          # Recruiter proposed availability for phone/video screen
    "APPLY_LINK_MANUAL",      # Complex portal link needing candidate direct inputs
    "COMPLETE_ASSESSMENT",    # HackerRank, Codility, take-home project
    "SEND_DOCUMENTS",         # Payslips, PF, degree, portfolio request
    "REVIEW_REPLY_DRAFT",     # Human-in-the-loop email reply pending approval
    "FOLLOW_UP_REMINDER",     # No response after N days from outreach
    "GENERAL"
]

PRIORITY_LEVELS = ["HIGH", "MEDIUM", "LOW"]


def load_tasks() -> dict:
    """Safely load persistent tasks ledger."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not os.path.exists(TASKS_FILE):
        return {"tasks": {}, "last_updated": ""}
    try:
        with open(TASKS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict) and "tasks" in data:
                return data
    except Exception as e:
        logging.error(f"[TaskManager] Error loading {TASKS_FILE}: {e}")
    return {"tasks": {}, "last_updated": ""}


def save_tasks(tasks_data: dict) -> bool:
    """Atomically write tasks ledger to disk."""
    tasks_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    temp_path = TASKS_FILE + ".tmp"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(tasks_data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(temp_path, TASKS_FILE)
        return True
    except Exception as e:
        logging.error(f"[TaskManager] Error saving {TASKS_FILE}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return False


def generate_task_id(prefix: str = "TASK") -> str:
    """Generate a clean human-readable timestamped task ID."""
    now = datetime.now()
    rand_suffix = os.urandom(2).hex().upper()
    return f"{prefix}-{now.strftime('%Y%m%d%H%M')}-{rand_suffix}"


def create_task(title: str, task_type: str = "GENERAL", recruiter_email: str = "",
                company: str = "", role: str = "", priority: str = "MEDIUM",
                description: str = "", action_url: str = "", due_date: str = "",
                metadata: dict = None, sync_reminders: bool = True) -> dict:
    """
    Create a new actionable candidate task.
    Deduplicates tasks with identical title and company to avoid redundant reminders.
    """
    tasks_data = load_tasks()
    tasks = tasks_data.setdefault("tasks", {})

    # Check for duplicate active task
    for existing in tasks.values():
        if (existing.get("status") == "PENDING" and
            existing.get("title", "").strip().lower() == title.strip().lower() and
            existing.get("company", "").strip().lower() == company.strip().lower()):
            logging.info(f"[TaskManager] Duplicate pending task already exists: {title} ({company})")
            return existing

    task_id = generate_task_id("TASK")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not due_date:
        # Default due date based on priority
        days = 1 if priority == "HIGH" else (3 if priority == "MEDIUM" else 5)
        due_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    task = {
        "id": task_id,
        "title": title,
        "type": task_type if task_type in TASK_TYPES else "GENERAL",
        "priority": priority if priority in PRIORITY_LEVELS else "MEDIUM",
        "company": company or "Unknown Company",
        "role": role or "Candidate",
        "recruiter_email": recruiter_email,
        "description": description,
        "action_url": action_url,
        "due_date": due_date,
        "status": "PENDING",
        "created_at": now_str,
        "completed_at": "",
        "metadata": metadata or {}
    }

    tasks[task_id] = task
    save_tasks(tasks_data)
    logging.info(f"[TaskManager] Created task [{task_id}] {title} (Priority: {priority})")

    if sync_reminders and priority in ("HIGH", "MEDIUM"):
        add_to_macos_reminders(task)

    return task


def complete_task(task_id: str, completion_note: str = "") -> bool:
    """Mark a task as COMPLETED."""
    tasks_data = load_tasks()
    task = tasks_data.get("tasks", {}).get(task_id)
    if not task:
        return False

    task["status"] = "COMPLETED"
    task["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if completion_note:
        task.setdefault("metadata", {})["completion_note"] = completion_note

    save_tasks(tasks_data)
    logging.info(f"[TaskManager] Completed task [{task_id}]")
    return True


def dismiss_task(task_id: str) -> bool:
    """Dismiss or cancel a task."""
    tasks_data = load_tasks()
    task = tasks_data.get("tasks", {}).get(task_id)
    if not task:
        return False

    task["status"] = "DISMISSED"
    task["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_tasks(tasks_data)
    logging.info(f"[TaskManager] Dismissed task [{task_id}]")
    return True


def reconcile_tasks_for_company(company: str = "", recruiter_email: str = "", domain: str = "",
                                target_status: str = "COMPLETED", note: str = "") -> int:
    """
    Auto-reconciles pending manual tasks when an application, interview, or rejection
    confirmation email is detected.
    """
    tasks_data = load_tasks()
    tasks = tasks_data.get("tasks", {})
    resolved = 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    comp_lower = company.strip().lower() if company else ""
    email_lower = recruiter_email.strip().lower() if recruiter_email else ""
    domain_lower = domain.strip().lower() if domain else ""

    for tid, t in tasks.items():
        if t.get("status") != "PENDING":
            continue

        t_comp = t.get("company", "").lower()
        t_email = t.get("recruiter_email", "").lower()
        t_url = t.get("action_url", "").lower()

        match = False
        if comp_lower and (comp_lower in t_comp or t_comp in comp_lower):
            match = True
        elif email_lower and email_lower == t_email:
            match = True
        elif domain_lower and domain_lower in t_url:
            match = True

        if match:
            t["status"] = target_status
            t["completed_at"] = now_str
            t.setdefault("metadata", {})["auto_reconciled"] = True
            if note:
                t["metadata"]["reconciliation_note"] = note
            resolved += 1
            logging.info(f"[TaskManager] Auto-reconciled task {tid} ({t.get('title')}) -> {target_status} [{note}]")

    if resolved > 0:
        save_tasks(tasks_data)

    return resolved


def get_pending_tasks(priority_sort: bool = True) -> list:
    """Return all pending tasks, optionally sorted by priority and due date."""
    tasks_data = load_tasks()
    pending = [t for t in tasks_data.get("tasks", {}).values() if t.get("status") == "PENDING"]

    if priority_sort:
        prio_weights = {"HIGH": 1, "MEDIUM": 2, "LOW": 3}
        pending.sort(key=lambda x: (prio_weights.get(x.get("priority", "MEDIUM"), 2), x.get("due_date", "9999")))

    return pending


def add_to_macos_reminders(task: dict) -> bool:
    """Optionally sync task to native macOS Reminders application via osascript."""
    try:
        title = task.get("title", "").replace('"', '\\"')
        notes = f"Company: {task.get('company')} | Role: {task.get('role')} | URL: {task.get('action_url')}".replace('"', '\\"')
        
        script = f'''
        tell application "Reminders"
            set targetList to list "Reminders"
            tell targetList
                make new reminder with properties {{name:"{title}", body:"{notes}"}}
            end tell
        end tell
        '''
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except Exception:
        # Non-blocking fallback
        return False


def print_tasks_table(include_completed: bool = False):
    """Render an ASCII table of pending tasks in the terminal."""
    tasks_data = load_tasks()
    all_tasks = list(tasks_data.get("tasks", {}).values())

    if not include_completed:
        tasks = [t for t in all_tasks if t.get("status") == "PENDING"]
    else:
        tasks = all_tasks

    prio_weights = {"HIGH": 1, "MEDIUM": 2, "LOW": 3}
    tasks.sort(key=lambda x: (0 if x.get("status") == "PENDING" else 1,
                              prio_weights.get(x.get("priority", "MEDIUM"), 2),
                              x.get("due_date", "")))

    print("\n" + "=" * 90)
    print(f"📋 CANDIDATE ACTION ITEMS & TASKS LEDGER ({len(tasks)} items)")
    print("=" * 90)

    header = f"{'ID':<18} {'PRIO':<8} {'TYPE':<20} {'COMPANY':<18} {'TITLE':<24}"
    print(header)
    print("-" * 90)

    prio_icons = {"HIGH": "🔴 HIGH", "MEDIUM": "🟡 MED", "LOW": "🟢 LOW"}
    for t in tasks:
        tid = t.get("id", "")[:17]
        prio = prio_icons.get(t.get("priority"), t.get("priority", "MED"))
        ttype = t.get("type", "GENERAL")[:19]
        co = t.get("company", "Company")[:17]
        title = t.get("title", "")[:24]
        st_mark = "" if t.get("status") == "PENDING" else " [DONE]"
        print(f"{tid:<18} {prio:<8} {ttype:<20} {co:<18} {title + st_mark:<24}")

    if not tasks:
        print("🎉 No pending tasks! All recruiter follow-ups are up to date.")
    print("=" * 90 + "\n")


def format_tasks_for_telegram() -> tuple:
    """Format pending tasks for Telegram notification with inline action buttons."""
    pending = get_pending_tasks(priority_sort=True)
    if not pending:
        return "🎉 *No Pending Tasks!*\nAll recruiter follow-ups, applications, and assessments are up to date.", None

    text_lines = [
        "📋 *PENDING CANDIDATE TASKS*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    inline_keyboard = []
    prio_emojis = {"HIGH": "🚨", "MEDIUM": "📌", "LOW": "💡"}

    for idx, t in enumerate(pending[:5], 1):
        emoji = prio_emojis.get(t.get("priority"), "📌")
        tid = t.get("id")
        title = t.get("title")
        co = t.get("company")
        due = t.get("due_date", "")[:10]
        url = t.get("action_url", "")

        text_lines.append(f"{emoji} *{idx}. {title}*")
        text_lines.append(f"   🏢 *Company:* `{co}` | 🗓️ *Due:* `{due}`")
        if url:
            text_lines.append(f"   🔗 [Open Link]({url})")
        text_lines.append("")

        # Add inline button row
        row = [
            {"text": f"✅ Mark Done #{idx}", "callback_data": f"complete_task:{tid}"}
        ]
        if url:
            row.append({"text": "🌐 Open Link", "url": url})
        inline_keyboard.append(row)

    if len(pending) > 5:
        text_lines.append(f"_... and {len(pending) - 5} more tasks. Use CLI `python3 run_all.py --tasks` to view all._")

    reply_markup = {"inline_keyboard": inline_keyboard} if inline_keyboard else None
    return "\n".join(text_lines), reply_markup


if __name__ == "__main__":
    import sys
    inc_comp = "--all" in sys.argv
    print_tasks_table(include_completed=inc_comp)
