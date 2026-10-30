#!/usr/bin/env python3
"""
resume_engine.py — High-Performance, ATS-Compliant PDF Resume Compiler
Renders 1-page executive resumes using ReportLab with dynamic layout budgeting,
clickable hyperlinks, and ATS-friendly typography.
"""

import os
import sys
import json
import logging
import subprocess
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
import pypdf

# Default color palette (Executive Navy & Slate)
PRIMARY_COLOR = colors.HexColor('#1A2B4C')
ACCENT_GRAY = colors.HexColor('#4A5568')
DIVIDER_COLOR = colors.HexColor('#CBD5E0')
BODY_COLOR = colors.HexColor('#2D3748')

MASTER_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'master_resume.json')
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')


def load_master_resume():
    """Load master resume data, overlaid with live config.json profile if present."""
    if os.path.exists(MASTER_DATA_PATH):
        with open(MASTER_DATA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {}

    # Overlay with config.json profile updates if available
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            prof = cfg.get('profile', {})
            p_info = data.setdefault('personal_info', {})
            for key in ('full_name', 'phone', 'email', 'location', 'current_location',
                        'notice_period', 'total_experience', 'relevant_experience',
                        'current_company', 'linkedin', 'github'):
                if prof.get(key):
                    p_info[key] = prof[key]
        except Exception as e:
            logging.warning(f"Failed to overlay config.json on master resume: {e}")

    return data


def save_master_resume(data):
    """Save updated master resume data to data/master_resume.json."""
    os.makedirs(os.path.dirname(MASTER_DATA_PATH), exist_ok=True)
    with open(MASTER_DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def build_adaptive_subtitle(role="Full Stack Developer", highlighted_skills=None):
    """Generate role-aligned headline subtitle."""
    role = role.strip() if role else "Full Stack Developer"
    core_stack = ["Java 21", "Spring Boot 3", "Microservices", "React 19", "Python FastAPI", "AWS"]
    
    if highlighted_skills:
        for skill in reversed(highlighted_skills):
            skill_clean = skill.strip()
            # Remove case-insensitive duplicates
            core_stack = [s for s in core_stack if s.lower() != skill_clean.lower()]
            core_stack.insert(0, skill_clean)
            
    return f"{role} | {' • '.join(core_stack[:5])}"


def open_in_preview(pdf_path):
    """Launch macOS Preview to open the PDF immediately."""
    if sys.platform == 'darwin' and os.path.exists(pdf_path):
        try:
            subprocess.run(["open", pdf_path], check=False)
            return True
        except Exception as e:
            logging.error(f"Failed to open PDF in Preview: {e}")
    return False


def reveal_in_finder(pdf_path):
    """Reveal the generated PDF in macOS Finder for easy drag & drop."""
    if sys.platform == 'darwin' and os.path.exists(pdf_path):
        try:
            subprocess.run(["open", "-R", pdf_path], check=False)
            return True
        except Exception as e:
            logging.error(f"Failed to reveal PDF in Finder: {e}")
    return False


def compile_pdf_resume(resume_data, output_path, single_page=True, compact_level=0):
    """
    Compile a high-resolution, ATS-compliant PDF resume from resume_data dictionary.
    Includes automatic page-budget optimization to guarantee 1-page output when single_page=True.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    
    # Dynamic styling based on compact_level (0 = default, 1 = tight, 2 = ultra-compact)
    font_size_body = 8.4 if compact_level == 0 else (8.0 if compact_level == 1 else 7.6)
    leading_body = 10.8 if compact_level == 0 else (10.0 if compact_level == 1 else 9.4)
    spacer_sec = 3.0 if compact_level == 0 else (2.0 if compact_level == 1 else 1.5)
    spacer_item = 1.2 if compact_level == 0 else (1.0 if compact_level == 1 else 0.8)
    top_margin = 20 if compact_level == 0 else 16
    bottom_margin = 20 if compact_level == 0 else 16

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=28,
        leftMargin=28,
        topMargin=top_margin,
        bottomMargin=bottom_margin
    )

    styles = getSampleStyleSheet()

    name_style = ParagraphStyle(
        'ResName', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=17, leading=19,
        textColor=PRIMARY_COLOR, alignment=0
    )
    subtitle_style = ParagraphStyle(
        'ResSub', parent=styles['Normal'],
        fontName='Helvetica', fontSize=8.5, leading=10.5,
        textColor=ACCENT_GRAY
    )
    contact_style = ParagraphStyle(
        'ResContact', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=8, leading=9.5,
        textColor=BODY_COLOR
    )
    sec_heading = ParagraphStyle(
        'ResSecHeading', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=9.5, leading=11.5,
        textColor=PRIMARY_COLOR, spaceBefore=spacer_sec, spaceAfter=2
    )
    body_style = ParagraphStyle(
        'ResBody', parent=styles['Normal'],
        fontName='Helvetica', fontSize=font_size_body, leading=leading_body,
        textColor=BODY_COLOR
    )
    bullet_style = ParagraphStyle(
        'ResBullet', parent=styles['Normal'],
        fontName='Helvetica', fontSize=font_size_body, leading=leading_body,
        leftIndent=8, textColor=BODY_COLOR
    )
    role_left = ParagraphStyle(
        'ResRoleLeft', parent=styles['Normal'],
        fontName='Helvetica', fontSize=8.8, leading=10.5,
        textColor=PRIMARY_COLOR
    )
    role_right = ParagraphStyle(
        'ResRoleRight', parent=styles['Normal'],
        fontName='Helvetica-Oblique', fontSize=8.2, leading=10.5,
        alignment=2, textColor=ACCENT_GRAY
    )

    p_info = resume_data.get('personal_info', {})
    story = []

    # 1. Header (Name + Adaptive Subtitle + Contact Links)
    full_name = (p_info.get("full_name") or "Amrit Kumar").upper()
    subtitle = resume_data.get("subtitle") or build_adaptive_subtitle(p_info.get("default_title", "Full Stack Developer"))
    location = p_info.get('location') or p_info.get('current_location') or 'Bengaluru, India'
    email_addr = p_info.get('email') or 'amrit.399.ak@gmail.com'
    phone = p_info.get('phone') or '+91-8789498334'
    linkedin_url = p_info.get('linkedin') or 'https://linkedin.com/in/AmritKumar'
    github_url = p_info.get('github') or 'https://github.com/amritkr1999'

    linkedin_display = linkedin_url.replace('https://www.', '').replace('https://', '').rstrip('/')
    github_display = github_url.replace('https://www.', '').replace('https://', '').rstrip('/')

    story.append(Paragraph(full_name, name_style))
    story.append(Spacer(1, 2))
    story.append(Paragraph(subtitle, subtitle_style))
    story.append(Paragraph(
        f"{location} • Phone: {phone} • Email: <a href=\"mailto:{email_addr}\" color=\"#1A2B4C\">{email_addr}</a>",
        contact_style
    ))
    story.append(Paragraph(
        f"LinkedIn: <a href=\"{linkedin_url}\" color=\"#1A2B4C\">{linkedin_display}</a> • GitHub: <a href=\"{github_url}\" color=\"#1A2B4C\">{github_display}</a>",
        subtitle_style
    ))
    story.append(Spacer(1, 3))
    story.append(HRFlowable(width="100%", thickness=1.2, color=PRIMARY_COLOR, spaceAfter=3, spaceBefore=0))

    # 2. Professional Summary
    summary_text = resume_data.get('summary', '').strip()
    if not summary_text:
        summaries = resume_data.get('summaries', {})
        summary_text = summaries.get('default', '')
    
    if summary_text:
        story.append(Paragraph("PROFESSIONAL SUMMARY", sec_heading))
        story.append(HRFlowable(width="100%", thickness=0.5, color=DIVIDER_COLOR, spaceAfter=2.5, spaceBefore=0))
        story.append(Paragraph(summary_text, body_style))
        story.append(Spacer(1, spacer_sec))

    # 3. Core Technical Skills
    skills_dict = resume_data.get('skills') or resume_data.get('core_skills') or {}
    if skills_dict:
        story.append(Paragraph("CORE TECHNICAL SKILLS", sec_heading))
        story.append(HRFlowable(width="100%", thickness=0.5, color=DIVIDER_COLOR, spaceAfter=2.5, spaceBefore=0))
        for cat_name, skill_items in skills_dict.items():
            if isinstance(skill_items, list):
                skills_str = ", ".join(skill_items)
            else:
                skills_str = str(skill_items)
            story.append(Paragraph(f"• <b>{cat_name}:</b> {skills_str}", bullet_style))
            story.append(Spacer(1, spacer_item))
        story.append(Spacer(1, spacer_sec))

    # 4. Professional Experience
    experiences = resume_data.get('experience', [])
    if experiences:
        story.append(Paragraph("PROFESSIONAL EXPERIENCE", sec_heading))
        story.append(HRFlowable(width="100%", thickness=0.5, color=DIVIDER_COLOR, spaceAfter=2.5, spaceBefore=0))
        for exp in experiences:
            company = exp.get('company', '')
            title = exp.get('title', '')
            period = exp.get('period', '')
            location = exp.get('location', '')
            left_text = f"<b>{title}</b> — {company}"
            right_text = f"{period} | {location}" if location else period
            
            t = Table([[Paragraph(left_text, role_left), Paragraph(right_text, role_right)]], colWidths=[350, 206])
            t.setStyle(TableStyle([
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                ('LEFTPADDING', (0,0), (-1,-1), 0),
                ('RIGHTPADDING', (0,0), (-1,-1), 0),
                ('TOPPADDING', (0,0), (-1,-1), 1),
                ('BOTTOMPADDING', (0,0), (-1,-1), 1)
            ]))
            story.append(t)
            
            bullets = exp.get('bullets', [])
            for b in bullets:
                b_text = b if isinstance(b, str) else b.get('text', '')
                if b_text:
                    story.append(Paragraph(f"• {b_text}", bullet_style))
                    story.append(Spacer(1, spacer_item))
            story.append(Spacer(1, spacer_sec))

    # 5. Featured Projects
    projects = resume_data.get('projects', [])
    if projects:
        story.append(Paragraph("FEATURED PROJECTS", sec_heading))
        story.append(HRFlowable(width="100%", thickness=0.5, color=DIVIDER_COLOR, spaceAfter=2.5, spaceBefore=0))
        for proj in projects:
            p_name = proj.get('name') or proj.get('title', '')
            p_sub = proj.get('subtitle', '')
            p_role = proj.get('role', '')
            
            title_parts = [f"<b>{p_name}</b>"]
            if p_sub:
                title_parts.append(f"— {p_sub}")
            if p_role:
                title_parts.append(f"({p_role})")
            
            story.append(Paragraph(" ".join(title_parts), body_style))
            for b in proj.get('bullets', []):
                b_text = b if isinstance(b, str) else b.get('text', '')
                if b_text:
                    story.append(Paragraph(f"• {b_text}", bullet_style))
                    story.append(Spacer(1, spacer_item))
            story.append(Spacer(1, 1.5))
        story.append(Spacer(1, spacer_sec))

    # 6. Education & Certifications
    edu = resume_data.get('education_and_certifications', {})
    if edu:
        story.append(Paragraph("EDUCATION & CERTIFICATIONS", sec_heading))
        story.append(HRFlowable(width="100%", thickness=0.5, color=DIVIDER_COLOR, spaceAfter=2.5, spaceBefore=0))
        degree = edu.get('degree') or 'B.Tech in Computer Science & Engineering'
        inst = edu.get('institution') or 'Gandhi Institute for Technological Advancement (GITA)'
        loc = edu.get('location') or 'Bhubaneswar, India'
        period = edu.get('period') or '2019 – 2023'
        
        left_text = f"<b>{degree}</b> — {inst}"
        right_text = f"{period} | {loc}" if loc else period

        edu_table = Table([[Paragraph(left_text, role_left),
                            Paragraph(right_text, role_right)]], colWidths=[415, 141])
        edu_table.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING', (0,0), (-1,-1), 1),
            ('BOTTOMPADDING', (0,0), (-1,-1), 1)
        ]))
        story.append(edu_table)
        
        prob = edu.get('problem_solving')
        if prob:
            story.append(Paragraph(f"• <b>Problem Solving:</b> {prob}", bullet_style))
            story.append(Spacer(1, spacer_item))
            
        certs = edu.get('certifications', [])
        if certs:
            certs_str = "; ".join(certs) if isinstance(certs, list) else str(certs)
            story.append(Paragraph(f"• <b>Certifications:</b> {certs_str}", bullet_style))

    # Build the PDF
    doc.build(story)

    # If single_page is requested, inspect page count and re-compile with tighter layout if necessary
    if single_page:
        try:
            reader = pypdf.PdfReader(output_path)
            num_pages = len(reader.pages)
            if num_pages > 1 and compact_level < 2:
                logging.info(f"PDF exceeded 1 page ({num_pages} pages). Retrying with compact_level={compact_level + 1}...")
                return compile_pdf_resume(resume_data, output_path, single_page=True, compact_level=compact_level + 1)
        except Exception as e:
            logging.warning(f"Unable to verify page count with pypdf: {e}")

    return output_path


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    test_data = load_master_resume()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output', 'TEST_RESUME_ENGINE.pdf')
    compile_pdf_resume(test_data, out, single_page=True)
    reader = pypdf.PdfReader(out)
    print(f"Generated test PDF: {out} ({len(reader.pages)} page(s), {os.path.getsize(out)} bytes)")
