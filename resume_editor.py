#!/usr/bin/env python3
"""
resume_editor.py — On-Demand Resume Editor & PDF Generator for Amrit Kumar
Enables instant customization of resume title, summary, skills, and experience
tailored to recruiter requests or raw Job Descriptions (JDs), compiling ATS-compliant
1-page PDFs with 1-click macOS Preview opening.

Usage:
  # One-liner tailored for a recruiter call:
  python resume_editor.py --company "Swiggy" --role "Senior Backend Engineer" --highlight "Kafka, Redis, Spring Boot" --open

  # Tailor using a pasted JD text file:
  python resume_editor.py --company "Razorpay" --jd-file jd.txt --open

  # Interactive Terminal Wizard:
  python resume_editor.py --interactive

  # Launch Local Web UI Editor:
  python resume_editor.py --web
"""

import os
import sys
import re
import json
import logging
import argparse
import webbrowser
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

from resume_engine import (
    load_master_resume,
    save_master_resume,
    compile_pdf_resume,
    build_adaptive_subtitle,
    open_in_preview,
    reveal_in_finder,
    PRIMARY_COLOR,
    CONFIG_PATH
)

# Common tech keywords for intelligent JD parsing
TECH_KEYWORDS = [
    "java", "java 21", "spring", "spring boot", "spring boot 3", "microservices",
    "kafka", "redis", "postgresql", "postgres", "mysql", "mongodb", "aws", "s3",
    "rds", "ec2", "cloudwatch", "docker", "kubernetes", "k8s", "react", "react 19",
    "python", "fastapi", "asyncio", "ollama", "llm", "ai", "rest", "restful", "api",
    "graphql", "jpa", "hibernate", "hikaricp", "distributed systems", "ci/cd",
    "github actions", "typescript", "node.js", "expo", "react native", "tailwind",
    "system design", "multithreading", "high throughput", "low latency", "b2b", "saas"
]


def extract_keywords_from_jd(jd_text):
    """Extract recognized technical skills and requirements from raw JD text."""
    if not jd_text:
        return []
    text_lower = jd_text.lower()
    found = []
    for kw in TECH_KEYWORDS:
        # Match whole word or exact term
        pattern = r'\b' + re.escape(kw) + r'\b'
        if re.search(pattern, text_lower):
            found.append(kw)
    
    # Capitalize appropriately
    capitalized = []
    mapping = {
        "java": "Java 21", "java 21": "Java 21", "spring": "Spring Boot 3",
        "spring boot": "Spring Boot 3", "spring boot 3": "Spring Boot 3",
        "microservices": "Microservices", "kafka": "Kafka", "redis": "Redis",
        "postgresql": "PostgreSQL", "postgres": "PostgreSQL", "mysql": "MySQL",
        "mongodb": "MongoDB", "aws": "AWS", "s3": "AWS S3", "rds": "AWS RDS",
        "ec2": "AWS EC2", "cloudwatch": "CloudWatch", "docker": "Docker",
        "kubernetes": "Kubernetes", "k8s": "Kubernetes", "react": "React 19",
        "react 19": "React 19", "python": "Python FastAPI", "fastapi": "FastAPI",
        "asyncio": "AsyncIO", "ollama": "Ollama AI", "llm": "LLMs", "ai": "AI Systems",
        "rest": "REST APIs", "restful": "RESTful APIs", "api": "API Design",
        "graphql": "GraphQL", "jpa": "JPA / Hibernate", "hibernate": "Hibernate",
        "hikaricp": "HikariCP", "distributed systems": "Distributed Systems",
        "ci/cd": "CI/CD", "github actions": "GitHub Actions", "typescript": "TypeScript",
        "node.js": "Node.js", "expo": "React Native (Expo)", "react native": "React Native",
        "system design": "System Design", "multithreading": "Multithreading",
        "high throughput": "High-Throughput Systems", "low latency": "Low-Latency Optimization",
        "b2b": "B2B SaaS"
    }
    seen = set()
    for f in found:
        mapped = mapping.get(f, f.title())
        if mapped not in seen:
            seen.add(mapped)
            capitalized.append(mapped)
    return capitalized


def generate_tailored_summary(master_data, role_title, key_reqs, jd_text="", preset_type=None):
    """Generate an impactful 2-3 sentence summary tailored to the target role and tech stack."""
    summaries = master_data.get('summaries', {})
    
    # If explicit preset requested and exists
    if preset_type and preset_type in summaries:
        return summaries[preset_type]

    # Check key requirements for smart automatic preset selection
    reqs_str = " ".join(key_reqs).lower() + " " + jd_text.lower() + " " + role_title.lower()
    
    if "ai" in reqs_str or "llm" in reqs_str or "vision" in reqs_str:
        base = summaries.get('ai_systems')
    elif "cloud" in reqs_str or "devops" in reqs_str or "docker" in reqs_str or "kubernetes" in reqs_str:
        base = summaries.get('cloud_devops')
    elif ("backend" in reqs_str or "java" in reqs_str or "kafka" in reqs_str) and "react" not in reqs_str:
        base = summaries.get('backend_java')
    elif "full stack" in reqs_str or "fullstack" in reqs_str:
        base = summaries.get('fullstack')
    else:
        base = summaries.get('default')

    if not base:
        base = summaries.get('default', '')

    # Try local Ollama if available for ultra-personalized summary
    try:
        import requests
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        host = cfg.get('ollama', {}).get('host', 'http://localhost:11434')
        model = cfg.get('ollama', {}).get('model', 'llama3.1:8b')
        prompt = f"""
Candidate: Amrit Kumar, 3+ years experience as Full Stack / Java Backend Developer.
Target Role: {role_title}
Key Requirements: {', '.join(key_reqs[:6])}

Write a high-impact, professional 2-3 sentence resume summary aligning Amrit's Java 21, Spring Boot, React 19, FastAPI, PostgreSQL, and AWS achievements with these requirements.
Keep under 50 words. Do NOT include company names or salary. Return ONLY the summary text.
"""
        resp = requests.post(
            f"{host.rstrip('/')}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.2}},
            timeout=10
        )
        if resp.status_code == 200:
            res_text = resp.json().get('response', '').strip()
            # Clean quotes
            res_text = re.sub(r'^["\']|["\']$', '', res_text).strip()
            if len(res_text) > 40:
                return res_text
    except Exception:
        pass

    return base


def reorder_skills_for_target(master_skills, key_reqs):
    """Reorder and prioritize skill categories based on target job requirements."""
    reqs_str = " ".join(key_reqs).lower()
    
    scored_cats = []
    for cat_name, skill_items in master_skills.items():
        skills_text = " ".join(skill_items).lower() if isinstance(skill_items, list) else str(skill_items).lower()
        score = 0
        for req in key_reqs:
            if req.lower() in skills_text or req.lower() in cat_name.lower():
                score += 3
        if "backend" in reqs_str and "Backend" in cat_name:
            score += 5
        if "cloud" in reqs_str and "Cloud" in cat_name:
            score += 4
        if "frontend" in reqs_str and "Frontend" in cat_name:
            score += 4
        if "ai" in reqs_str and "AI" in cat_name:
            score += 4
        scored_cats.append((score, cat_name, skill_items))
        
    scored_cats.sort(key=lambda x: x[0], reverse=True)
    return {cat: items for _, cat, items in scored_cats}


def filter_and_rank_bullets(bullets, key_reqs, max_bullets=3):
    """Score and select the top N experience bullets matching key requirements."""
    reqs_set = {r.lower() for r in key_reqs}
    scored = []
    for idx, b in enumerate(bullets):
        text = b.get('text', '') if isinstance(b, dict) else str(b)
        tags = b.get('tags', []) if isinstance(b, dict) else []
        tag_hits = sum(1 for t in tags if any(t in r or r in t for r in reqs_set))
        text_hits = sum(1 for r in reqs_set if r in text.lower())
        score = (tag_hits * 2) + text_hits
        scored.append((score, -idx, text))
        
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [item[2] for item in scored[:max_bullets]]


def select_top_projects(projects, key_reqs, max_projects=3):
    """Select the top projects most relevant to the target requirements."""
    reqs_set = {r.lower() for r in key_reqs}
    scored = []
    for idx, proj in enumerate(projects):
        tags = proj.get('tags', [])
        p_name = proj.get('name', '')
        score = sum(2 for t in tags if any(t in r or r in t for r in reqs_set))
        if any(r in p_name.lower() for r in reqs_set):
            score += 2
        scored.append((score, -idx, proj))
        
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [item[2] for item in scored[:max_projects]]


def create_tailored_resume_data(
    company_name="",
    role_title="",
    highlight_skills=None,
    jd_text="",
    summary_override="",
    summary_preset=None,
    custom_bullet=None,
    custom_company_bullet=None
):
    """Build a complete tailored resume dictionary ready for PDF compilation."""
    master = load_master_resume()
    
    # 1. Determine Target Role Title
    if not role_title:
        role_title = master.get('personal_info', {}).get('default_title', 'Full Stack Developer')
        
    # 2. Extract Key Requirements
    key_reqs = []
    if highlight_skills:
        if isinstance(highlight_skills, str):
            key_reqs.extend([s.strip() for s in highlight_skills.split(',') if s.strip()])
        elif isinstance(highlight_skills, list):
            key_reqs.extend(highlight_skills)
            
    if jd_text:
        jd_reqs = extract_keywords_from_jd(jd_text)
        for r in jd_reqs:
            if r not in key_reqs:
                key_reqs.append(r)
                
    if not key_reqs:
        key_reqs = ["Java 21", "Spring Boot 3", "Microservices", "React 19", "PostgreSQL", "AWS"]

    # 3. Build Subtitle
    subtitle = build_adaptive_subtitle(role_title, key_reqs)
    
    # 4. Determine Professional Summary
    if summary_override:
        summary = summary_override.strip()
    else:
        summary = generate_tailored_summary(master, role_title, key_reqs, jd_text, summary_preset)
        
    # 5. Tailor Skills
    master_skills = master.get('core_skills', {})
    tailored_skills = reorder_skills_for_target(master_skills, key_reqs)
    
    # 6. Tailor Experience
    tailored_exp = []
    for exp in master.get('experience', []):
        exp_copy = dict(exp)
        original_bullets = exp.get('bullets', [])
        ranked_bullets = filter_and_rank_bullets(original_bullets, key_reqs, max_bullets=3)
        
        # Inject custom bullet if provided for this company
        if custom_bullet and (not custom_company_bullet or custom_company_bullet.lower() in exp.get('company', '').lower()):
            ranked_bullets.insert(0, custom_bullet)
            ranked_bullets = ranked_bullets[:4]
            
        exp_copy['bullets'] = ranked_bullets
        tailored_exp.append(exp_copy)
        
    # 7. Tailor Featured Projects
    master_projects = master.get('projects', [])
    tailored_projects = select_top_projects(master_projects, key_reqs, max_projects=3)
    
    return {
        "company_name": company_name,
        "target_role": role_title,
        "personal_info": master.get('personal_info', {}),
        "subtitle": subtitle,
        "summary": summary,
        "skills": tailored_skills,
        "experience": tailored_exp,
        "projects": tailored_projects,
        "education_and_certifications": master.get('education_and_certifications', {})
    }


def generate_resume_pdf(
    company_name="",
    role_title="",
    highlight_skills=None,
    jd_text="",
    summary_override="",
    summary_preset=None,
    custom_bullet=None,
    custom_company_bullet=None,
    output_path=None,
    open_preview=False,
    reveal=False
):
    """Complete workflow: tailor resume data, compile 1-page PDF, and launch macOS Preview/Finder."""
    tailored = create_tailored_resume_data(
        company_name=company_name,
        role_title=role_title,
        highlight_skills=highlight_skills,
        jd_text=jd_text,
        summary_override=summary_override,
        summary_preset=summary_preset,
        custom_bullet=custom_bullet,
        custom_company_bullet=custom_company_bullet
    )

    # Determine Output Path
    if not output_path:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
        os.makedirs(out_dir, exist_ok=True)
        if company_name:
            clean_co = re.sub(r'[^a-zA-Z0-9_-]', '_', company_name.strip()).strip('_')
            filename = f"AMRIT_KUMAR_{clean_co}_Resume.pdf"
        else:
            filename = "AMRIT_KUMAR_Resume.pdf"
        output_path = os.path.join(out_dir, filename)

    # Compile PDF with 1-page guarantee
    compile_pdf_resume(tailored, output_path, single_page=True)
    
    # macOS triggers
    if open_preview:
        open_in_preview(output_path)
    if reveal:
        reveal_in_finder(output_path)
        
    return output_path, tailored


# -----------------------------------------------------------------------------
# Interactive Terminal Wizard
# -----------------------------------------------------------------------------

def run_interactive_wizard():
    """Step-by-step interactive command-line wizard."""
    print("\n" + "=" * 65)
    print("      AMRIT KUMAR — RESUME EDITOR & PDF GENERATOR")
    print("=" * 65)
    print("1. Tailor Resume for Recruiter / Company (JD or Tech Stack)")
    print("2. Paste Full Job Description Text (Auto-Analyze & Tailor)")
    print("3. Generate Fresh Master Resume PDF")
    print("4. Edit Master Resume Data (Title, Skills, Bullets, Contact)")
    print("5. Launch Local Web UI Editor (Browser)")
    print("6. Open Last Generated PDF in macOS Preview")
    print("7. Exit")
    print("=" * 65)

    choice = input("\nSelect an option [1-7]: ").strip()
    
    if choice == '1':
        company = input("Company Name (e.g. Swiggy, Google, Zeta): ").strip()
        role = input("Target Role (default: Full Stack Developer): ").strip() or "Full Stack Developer"
        skills = input("Skills to Emphasize (comma-separated, e.g. Kafka, Redis, AWS): ").strip()
        custom_sum = input("Custom Summary Override (leave blank for auto-tailored): ").strip()
        
        print("\n⚙️  Tailoring resume & compiling 1-page PDF...")
        pdf_path, tailored = generate_resume_pdf(
            company_name=company,
            role_title=role,
            highlight_skills=skills,
            summary_override=custom_sum,
            open_preview=True
        )
        print(f"\n✅ Resume successfully generated and opened in macOS Preview!")
        print(f"📄 PDF Location: {pdf_path}")
        print(f"🎯 Target Title: {tailored['subtitle']}")
        print(f"📝 Summary:\n   {tailored['summary']}")

    elif choice == '2':
        company = input("Company Name (optional): ").strip()
        role = input("Target Role (leave blank to auto-detect): ").strip()
        print("\nPaste Job Description below. Press ENTER then Ctrl+D (EOF) when finished:")
        jd_lines = sys.stdin.read().strip()
        
        print("\n⚙️  Analyzing JD, extracting keywords & compiling PDF...")
        pdf_path, tailored = generate_resume_pdf(
            company_name=company,
            role_title=role or "Full Stack Developer",
            jd_text=jd_lines,
            open_preview=True
        )
        print(f"\n✅ Tailored Resume successfully generated and opened in macOS Preview!")
        print(f"📄 PDF Location: {pdf_path}")
        print(f"🎯 Target Title: {tailored['subtitle']}")
        print(f"📝 Tailored Summary:\n   {tailored['summary']}")

    elif choice == '3':
        print("\n⚙️  Compiling fresh Master Resume PDF...")
        pdf_path, _ = generate_resume_pdf(
            company_name="",
            role_title="Full Stack Developer",
            open_preview=True
        )
        print(f"\n✅ Fresh Master Resume generated and opened!")
        print(f"📄 Location: {pdf_path}")

    elif choice == '4':
        master = load_master_resume()
        print("\n--- Current Personal Info ---")
        p = master.get('personal_info', {})
        print(f"Name: {p.get('full_name')} | Title: {p.get('default_title')} | Phone: {p.get('phone')}")
        print(f"Email: {p.get('email')} | Notice Period: {p.get('notice_period')}")
        
        new_title = input(f"New Default Title [{p.get('default_title')}]: ").strip()
        if new_title:
            p['default_title'] = new_title
        new_phone = input(f"New Phone [{p.get('phone')}]: ").strip()
        if new_phone:
            p['phone'] = new_phone
        new_notice = input(f"New Notice Period [{p.get('notice_period')}]: ").strip()
        if new_notice:
            p['notice_period'] = new_notice
            
        save_master_resume(master)
        print("✅ Master resume data updated successfully!")

    elif choice == '5':
        start_web_server(open_browser=True)

    elif choice == '6':
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
        pdfs = [os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith('.pdf')]
        if pdfs:
            latest_pdf = max(pdfs, key=os.path.getmtime)
            print(f"Opening {latest_pdf} in Preview...")
            open_in_preview(latest_pdf)
        else:
            print("No generated PDFs found in output directory.")

    elif choice == '7':
        print("Goodbye!")
        sys.exit(0)
    else:
        print("Invalid choice.")


# -----------------------------------------------------------------------------
# Embedded Lightweight Web UI
# -----------------------------------------------------------------------------

WEB_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Amrit Kumar — Resume Editor & PDF Generator</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f0f2f5; color: #1a202c; }
    header { background: #1a2b4c; color: white; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
    header h1 { font-size: 20px; font-weight: 700; letter-spacing: 0.5px; }
    .badge { background: #2b6cb0; font-size: 11px; padding: 4px 10px; border-radius: 12px; font-weight: 600; }
    .container { display: flex; height: calc(100vh - 60px); }
    .editor-pane { width: 50%; padding: 24px; overflow-y: auto; background: white; border-right: 1px solid #e2e8f0; }
    .preview-pane { width: 50%; padding: 24px; overflow-y: auto; background: #e2e8f0; display: flex; flex-direction: column; align-items: center; }
    
    .section-title { font-size: 14px; font-weight: 700; text-transform: uppercase; color: #1a2b4c; border-bottom: 2px solid #cbd5e0; padding-bottom: 4px; margin-bottom: 14px; margin-top: 20px; }
    .section-title:first-child { margin-top: 0; }
    
    label { display: block; font-size: 12px; font-weight: 600; color: #4a5568; margin-bottom: 4px; }
    input[type="text"], textarea, select { width: 100%; padding: 8px 12px; border: 1px solid #cbd5e0; border-radius: 6px; font-size: 13px; margin-bottom: 14px; }
    input[type="text"]:focus, textarea:focus, select:focus { outline: none; border-color: #1a2b4c; box-shadow: 0 0 0 2px rgba(26,43,76,0.15); }
    textarea { resize: vertical; min-height: 80px; }
    
    .row { display: flex; gap: 12px; }
    .col { flex: 1; }
    
    .btn { display: inline-flex; align-items: center; justify-content: center; padding: 10px 18px; font-size: 13px; font-weight: 600; border-radius: 6px; cursor: pointer; border: none; transition: background 0.15s ease; text-decoration: none; }
    .btn-primary { background: #1a2b4c; color: white; }
    .btn-primary:hover { background: #2c4373; }
    .btn-success { background: #2e7d32; color: white; }
    .btn-success:hover { background: #1b5e20; }
    .btn-secondary { background: #edf2f7; color: #2d3748; }
    .btn-secondary:hover { background: #e2e8f0; }
    
    .actions { display: flex; gap: 10px; margin-top: 18px; }
    
    /* Paper Mockup */
    .paper { width: 100%; max-width: 580px; background: white; padding: 32px; box-shadow: 0 8px 24px rgba(0,0,0,0.12); border-radius: 4px; font-size: 11px; line-height: 1.4; color: #2d3748; }
    .paper h2 { color: #1a2b4c; font-size: 18px; font-weight: 800; letter-spacing: 0.5px; }
    .paper .sub { color: #4a5568; font-size: 10px; margin-top: 2px; }
    .paper .contact { font-weight: 700; font-size: 9.5px; margin-top: 4px; }
    .paper hr { border: none; border-top: 1.5px solid #1a2b4c; margin: 8px 0; }
    .paper .p-sec { font-weight: 700; font-size: 11px; color: #1a2b4c; border-bottom: 0.5px solid #cbd5e0; padding-bottom: 2px; margin-top: 8px; margin-bottom: 4px; }
    .paper ul { padding-left: 14px; margin: 3px 0; }
    .paper li { margin-bottom: 2px; }
    
    .toast { position: fixed; bottom: 20px; right: 20px; background: #2e7d32; color: white; padding: 12px 20px; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.2); font-size: 13px; font-weight: 600; display: none; }
  </style>
</head>
<body>
  <header>
    <div style="display: flex; align-items: center; gap: 12px;">
      <h1>Amrit Kumar — Resume Editor</h1>
      <span class="badge">ATS 1-Page Engine</span>
    </div>
    <div style="font-size: 12px; opacity: 0.9;">
      Java 21 • Spring Boot 3 • React 19 • Python FastAPI • AWS
    </div>
  </header>

  <div class="container">
    <!-- Left Panel: Editor Form -->
    <div class="editor-pane">
      <div class="section-title">1. Recruiter & Job Targeting</div>
      <div class="row">
        <div class="col">
          <label>Target Company</label>
          <input type="text" id="company" placeholder="e.g. Swiggy, Google, Zeta">
        </div>
        <div class="col">
          <label>Target Role Title</label>
          <input type="text" id="roleTitle" value="Full Stack Developer">
        </div>
      </div>

      <label>Skills to Emphasize (comma-separated)</label>
      <input type="text" id="highlightSkills" placeholder="e.g. Kafka, Redis, Microservices, AWS">

      <label>Paste Raw Job Description (optional auto-tailoring)</label>
      <textarea id="jdText" placeholder="Paste JD requirements or recruiter message here..."></textarea>

      <div class="section-title">2. Professional Summary</div>
      <label>Preset Summary Type</label>
      <select id="summaryPreset" onchange="applyPreset()">
        <option value="">Custom / Auto-Tailored</option>
        <option value="default">Default Full Stack</option>
        <option value="backend_java">Senior Java Backend (Kafka, Microservices)</option>
        <option value="fullstack">Full Stack Engineer (React 19, Spring Boot)</option>
        <option value="cloud_devops">Cloud & DevOps (AWS, Docker, CI/CD)</option>
        <option value="ai_systems">AI Systems (FastAPI, Ollama, Gemini)</option>
      </select>
      <label>Summary Content</label>
      <textarea id="summaryText" rows="3"></textarea>

      <div class="actions">
        <button class="btn btn-primary" onclick="generateResume(true)">🚀 Generate & Open in Preview</button>
        <button class="btn btn-secondary" onclick="updateLivePreview()">🔄 Refresh Preview</button>
      </div>
    </div>

    <!-- Right Panel: Live Preview & Downloads -->
    <div class="preview-pane">
      <div style="width: 100%; max-width: 580px; display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
        <span style="font-weight: 700; color: #4a5568; font-size: 13px;">Live Resume Preview (Letter Size)</span>
        <button class="btn btn-success" onclick="generateResume(false)">💾 Download PDF</button>
      </div>

      <div class="paper" id="paper">
        <h2 id="prevName">CANDIDATE NAME</h2>
        <div class="sub" id="prevSubtitle">Full Stack Software Engineer | Microservices • Cloud Architecture • Web Applications</div>
        <div class="contact" id="prevContact">Mobile: +91-9876543210 | Email: candidate@example.com</div>
        <div class="sub" id="prevLinks">LinkedIn: https://linkedin.com/in/yourprofile | GitHub: https://github.com/yourusername</div>
        <hr>

        <div class="p-sec">PROFESSIONAL SUMMARY</div>
        <p id="prevSummary">Loading master profile...</p>

        <div class="p-sec">CORE TECHNICAL SKILLS</div>
        <ul id="prevSkills"></ul>

        <div class="p-sec">PROFESSIONAL EXPERIENCE</div>
        <div id="prevExp"></div>

        <div class="p-sec">FEATURED PROJECTS</div>
        <div id="prevProjects"></div>

        <div class="p-sec">EDUCATION & CERTIFICATIONS</div>
        <p><b>Bachelor of Technology (B.Tech) – CSE</b> — State University (2019 – 2023)</p>
        <p>• <b>Problem Solving:</b> Solved 200+ algorithm and system design problems.</p>
      </div>
    </div>
  </div>

  <div class="toast" id="toast">✅ PDF generated successfully!</div>

  <script>
    let masterData = {};

    async function loadInitial() {
      const res = await fetch('/api/resume');
      masterData = await res.json();
      document.getElementById('summaryText').value = masterData.summaries?.default || '';
      updateLivePreview();
    }

    function applyPreset() {
      const val = document.getElementById('summaryPreset').value;
      if (val && masterData.summaries && masterData.summaries[val]) {
        document.getElementById('summaryText').value = masterData.summaries[val];
      }
      updateLivePreview();
    }

    function updateLivePreview() {
      const role = document.getElementById('roleTitle').value || 'Full Stack Developer';
      const skills = document.getElementById('highlightSkills').value.split(',').map(s => s.trim()).filter(Boolean);
      const coreStack = ["Java 21", "Spring Boot 3", "Microservices", "React 19", "Python FastAPI"];
      
      const combined = [...skills, ...coreStack].filter((v, i, a) => a.indexOf(v) === i).slice(0, 5);
      document.getElementById('prevSubtitle').innerText = `${role} | ${combined.join(' • ')}`;
      document.getElementById('prevSummary').innerText = document.getElementById('summaryText').value || masterData.summaries?.default || '';

      // Skills
      const sUl = document.getElementById('prevSkills');
      sUl.innerHTML = '';
      if (masterData.core_skills) {
        for (const [k, v] of Object.entries(masterData.core_skills)) {
          const li = document.createElement('li');
          li.innerHTML = `<b>${k}:</b> ${Array.isArray(v) ? v.join(', ') : v}`;
          sUl.appendChild(li);
        }
      }

      // Experience
      const expDiv = document.getElementById('prevExp');
      expDiv.innerHTML = '';
      if (masterData.experience) {
        masterData.experience.forEach(e => {
          const block = document.createElement('div');
          block.style.marginBottom = '6px';
          block.innerHTML = `<div style="display:flex; justify-content:space-between; font-weight:700;">
            <span>${e.title} — ${e.company}</span>
            <span style="font-weight:normal; font-style:italic;">${e.period}</span>
          </div>`;
          const ul = document.createElement('ul');
          (e.bullets || []).slice(0, 3).forEach(b => {
            const li = document.createElement('li');
            li.innerText = typeof b === 'string' ? b : b.text;
            ul.appendChild(li);
          });
          block.appendChild(ul);
          expDiv.appendChild(block);
        });
      }

      // Projects
      const projDiv = document.getElementById('prevProjects');
      projDiv.innerHTML = '';
      if (masterData.projects) {
        masterData.projects.slice(0, 3).forEach(p => {
          const block = document.createElement('div');
          block.style.marginBottom = '5px';
          block.innerHTML = `<b>${p.name}</b> — ${p.subtitle} (${p.role})`;
          const ul = document.createElement('ul');
          (p.bullets || []).forEach(b => {
            const li = document.createElement('li');
            li.innerText = b;
            ul.appendChild(li);
          });
          block.appendChild(ul);
          projDiv.appendChild(block);
        });
      }
    }

    async function generateResume(openPreview) {
      const payload = {
        company_name: document.getElementById('company').value,
        role_title: document.getElementById('roleTitle').value,
        highlight_skills: document.getElementById('highlightSkills').value,
        jd_text: document.getElementById('jdText').value,
        summary_override: document.getElementById('summaryText').value,
        open_preview: openPreview
      };

      const res = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.status === 'ok') {
        showToast(openPreview ? '✅ PDF compiled & opened in Preview!' : '✅ PDF compiled!');
        if (!openPreview) {
          window.location.href = `/api/download?path=${encodeURIComponent(data.pdf_path)}`;
        }
      }
    }

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 3500);
    }

    document.addEventListener('DOMContentLoaded', loadInitial);
  </script>
</body>
</html>
"""


class ResumeEditorHTTPHandler(BaseHTTPRequestHandler):
    """Local HTTP Server handler for Resume Editor Web UI."""

    def log_message(self, format, *args):
        # Silence verbose request logs
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/' or path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(WEB_HTML_TEMPLATE.encode('utf-8'))

        elif path == '/api/resume':
            data = load_master_resume()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))

        elif path == '/api/download':
            query = urllib.parse.parse_qs(parsed.query)
            pdf_path = query.get('path', [''])[0]
            if pdf_path and os.path.exists(pdf_path):
                self.send_response(200)
                self.send_header('Content-Type', 'application/pdf')
                self.send_header('Content-Disposition', f'attachment; filename="{os.path.basename(pdf_path)}"')
                self.end_headers()
                with open(pdf_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/generate':
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len)
            payload = json.loads(body.decode('utf-8'))

            pdf_path, tailored = generate_resume_pdf(
                company_name=payload.get('company_name', ''),
                role_title=payload.get('role_title', ''),
                highlight_skills=payload.get('highlight_skills', ''),
                jd_text=payload.get('jd_text', ''),
                summary_override=payload.get('summary_override', ''),
                open_preview=payload.get('open_preview', False)
            )

            res = {
                "status": "ok",
                "pdf_path": pdf_path,
                "filename": os.path.basename(pdf_path)
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(res).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()


def start_web_server(port=8765, open_browser=True):
    """Run local Web UI server."""
    server_address = ('127.0.0.1', port)
    httpd = HTTPServer(server_address, ResumeEditorHTTPHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"\n🌐 Resume Editor Web UI running at: {url}")
    print("Press Ctrl+C to stop the server.\n")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Web UI server...")
        httpd.server_close()


# -----------------------------------------------------------------------------
# Main CLI Entrypoint
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="On-Demand Resume Editor & PDF Generator for Amrit Kumar",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick tailor for recruiter call:
  python resume_editor.py --company "Swiggy" --role "Senior Backend Engineer" --highlight "Kafka, Redis, Spring Boot" --open

  # Tailor from JD file:
  python resume_editor.py --company "Razorpay" --jd-file jd.txt --open

  # Launch Interactive CLI Wizard:
  python resume_editor.py -i

  # Launch Local Web UI:
  python resume_editor.py --web
        """
    )
    parser.add_argument("--company", "-c", type=str, default="", help="Target company name (e.g. Swiggy, Google)")
    parser.add_argument("--role", "-r", type=str, default="", help="Target job role title (e.g. Senior Java Developer)")
    parser.add_argument("--highlight", "-k", type=str, default="", help="Comma-separated skills to emphasize (e.g. Kafka, Redis, AWS)")
    parser.add_argument("--jd", type=str, default="", help="Raw Job Description string")
    parser.add_argument("--jd-file", type=str, default="", help="Path to text file containing Job Description")
    parser.add_argument("--summary", "-s", type=str, default="", help="Direct override for professional summary")
    parser.add_argument("--summary-preset", type=str, choices=['default', 'backend_java', 'fullstack', 'cloud_devops', 'ai_systems'], help="Use pre-crafted summary preset")
    parser.add_argument("--bullet", type=str, default="", help="Custom achievement bullet to inject into top role")
    parser.add_argument("--output", "-o", type=str, default="", help="Custom output PDF path")
    parser.add_argument("--open", action="store_true", default=True, help="Automatically open generated PDF in macOS Preview (default: True)")
    parser.add_argument("--no-open", dest="open", action="store_false", help="Do not open PDF in Preview")
    parser.add_argument("--reveal", action="store_true", help="Reveal generated PDF in macOS Finder")
    parser.add_argument("--web", action="store_true", help="Launch local Web UI editor in browser")
    parser.add_argument("--port", type=int, default=8765, help="Port for local Web UI (default: 8765)")
    parser.add_argument("--interactive", "-i", action="store_true", help="Launch step-by-step terminal wizard")

    args = parser.parse_args()

    # Web Mode
    if args.web:
        start_web_server(port=args.port, open_browser=True)
        return

    # Interactive Wizard Mode
    if args.interactive or (len(sys.argv) == 1 and sys.stdin.isatty()):
        run_interactive_wizard()
        return

    # Read JD file if provided
    jd_content = args.jd
    if args.jd_file and os.path.exists(args.jd_file):
        with open(args.jd_file, 'r', encoding='utf-8') as f:
            jd_content = f.read()

    # Generate Tailored PDF
    print(f"\n⚙️  Generating tailored resume for: {args.company or 'General Application'}...")
    pdf_path, tailored = generate_resume_pdf(
        company_name=args.company,
        role_title=args.role,
        highlight_skills=args.highlight,
        jd_text=jd_content,
        summary_override=args.summary,
        summary_preset=args.summary_preset,
        custom_bullet=args.bullet,
        output_path=args.output,
        open_preview=args.open,
        reveal=args.reveal
    )

    print("\n" + "=" * 65)
    print("               RESUME GENERATED SUCCESSFULLY")
    print("=" * 65)
    print(f"📄 Output PDF : {pdf_path}")
    print(f"🎯 Subtitle   : {tailored['subtitle']}")
    print(f"📝 Summary    :\n   {tailored['summary']}")
    if args.open:
        print("🖥️  Opened in macOS Preview.")
    if args.reveal:
        print("📂 Revealed in macOS Finder.")
    print("=" * 65 + "\n")


if __name__ == '__main__':
    main()
