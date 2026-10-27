#!/usr/bin/env python3
"""
Pending QA Review & Asynchronous Learning Manager (pending_qa.py)
-----------------------------------------------------------------
Captures any screening question that timed out and was answered with a default,
allowing Amrit to review and customize the preferred answer later at any time
via Telegram (/review) or the Mission Control Web Dashboard.

Once answered, the new answer is permanently committed to `learned_qa_bank.json`
so all future applications across all portals automatically use Amrit's preference.
"""

import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

import qa_memory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
PENDING_FILE = os.path.join(LOGS_DIR, 'pending_qa_reviews.json')


def _make_question_id(question: str) -> str:
    """Generate a clean, reproducible short ID for a question."""
    norm = re.sub(r'[^a-z0-9]+', '', question.lower())
    short_hash = hashlib.md5(question.encode('utf-8')).hexdigest()[:6]
    return f"qa_{short_hash}"


def load_pending_reviews() -> Dict[str, Any]:
    """Load pending reviews store from disk safely."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict) and "questions" in data:
                    return data
        except Exception as e:
            logging.warning(f"[Pending QA] Failed reading {PENDING_FILE}: {e}")

    return {
        "version": 1,
        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "questions": {}
    }


def save_pending_reviews(data: Dict[str, Any]) -> bool:
    """Save pending reviews store to disk atomically."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    data["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp_path = f"{PENDING_FILE}.tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_path, PENDING_FILE)
        return True
    except Exception as e:
        logging.error(f"[Pending QA] Failed writing {PENDING_FILE}: {e}")
        return False


def add_pending_question(question: str,
                         options: Optional[List[str]] = None,
                         platform: str = "ATS",
                         company: str = "Company",
                         role: str = "Application",
                         default_answered: str = "") -> Dict[str, Any]:
    """
    Queue a question that was default-answered for later human review.
    """
    if not question or not str(question).strip():
        return {}

    qid = _make_question_id(question)
    store = load_pending_reviews()

    entry = {
        "id": qid,
        "question": str(question).strip(),
        "options": options or [],
        "platform": platform or "ATS",
        "company": company or "Company",
        "role": role or "Application",
        "default_answered": str(default_answered or "").strip(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "PENDING",
        "times_asked": store.get("questions", {}).get(qid, {}).get("times_asked", 0) + 1
    }

    store["questions"][qid] = entry
    save_pending_reviews(store)
    logging.info(f"[Pending QA] 📥 Queued question for later review: '{question[:45]}...' (ID: {qid}, Default: {default_answered})")
    return entry


def get_pending_questions(status: str = "PENDING") -> List[Dict[str, Any]]:
    """Return all pending questions, sorted newest first."""
    store = load_pending_reviews()
    items = list(store.get("questions", {}).values())
    if status:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return items


def resolve_pending_question(question_id: str, user_answer: str) -> bool:
    """
    Apply Amrit's custom answer to a pending question:
    1. Permanently commits to `learned_qa_bank.json`.
    2. Marks pending review entry as 'RESOLVED'.
    """
    if not question_id or not user_answer or str(user_answer).strip() in ("", "SKIP"):
        return False

    clean_ans = str(user_answer).strip()
    store = load_pending_reviews()
    questions = store.get("questions", {})

    target = questions.get(question_id)
    if not target:
        # Search by question text match
        for qid, qdata in questions.items():
            if qdata.get("question") == question_id or qid == question_id:
                target = qdata
                question_id = qid
                break

    if not target:
        return False

    q_text = target.get("question", "")
    platform = target.get("platform", "User Review")

    # 1. Commit to persistent QA Memory Bank
    qa_memory.save_learned_answer(q_text, clean_ans, platform=platform)

    # 2. Mark as resolved
    target["status"] = "RESOLVED"
    target["user_answer"] = clean_ans
    target["resolved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    save_pending_reviews(store)
    logging.info(f"[Pending QA] ✅ Resolved '{q_text[:40]}...' with user choice: '{clean_ans}'")
    return True


def dismiss_pending_question(question_id: str) -> bool:
    """Dismiss a pending review without modifying QA bank."""
    store = load_pending_reviews()
    if question_id in store.get("questions", {}):
        store["questions"][question_id]["status"] = "DISMISSED"
        store["questions"][question_id]["dismissed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_pending_reviews(store)
        return True
    return False


def get_pending_stats() -> Dict[str, Any]:
    """Return high-level counts for dashboard and telegram headers."""
    store = load_pending_reviews()
    all_q = list(store.get("questions", {}).values())
    pending = [q for q in all_q if q.get("status") == "PENDING"]
    resolved = [q for q in all_q if q.get("status") == "RESOLVED"]

    mem = qa_memory.load_qa_memory()
    total_learned = len(mem.get("exact_questions", {}))

    return {
        "pending_count": len(pending),
        "resolved_count": len(resolved),
        "total_learned_answers": total_learned
    }
