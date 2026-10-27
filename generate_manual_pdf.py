#!/usr/bin/env python3
"""
Production-grade PDF Generator for SYSTEM_MANUAL.md
Using ReportLab, TrueType typography, high-res visual architecture diagrams,
Preformatted monospace code boxes, and exact PyMuPDF outline bookmarks.
"""

import os
import re
import textwrap
import fitz
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
    Preformatted,
    Image as RLImage
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from PIL import Image, ImageDraw, ImageFont

# ==============================================================================
# 1. REGISTER FONTS
# ==============================================================================
def setup_fonts():
    font_paths = {
        'Arial': '/System/Library/Fonts/Supplemental/Arial.ttf',
        'Arial-Bold': '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
        'Arial-Italic': '/System/Library/Fonts/Supplemental/Arial Italic.ttf',
        'Arial-BoldItalic': '/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf',
        'CourierNew': '/System/Library/Fonts/Supplemental/Courier New.ttf',
        'CourierNew-Bold': '/System/Library/Fonts/Supplemental/Courier New Bold.ttf',
        'CourierNew-Italic': '/System/Library/Fonts/Supplemental/Courier New Italic.ttf',
    }
    for name, path in font_paths.items():
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont(name, path))
    
    pdfmetrics.registerFontFamily(
        'Arial',
        normal='Arial',
        bold='Arial-Bold',
        italic='Arial-Italic',
        boldItalic='Arial-BoldItalic'
    )
    pdfmetrics.registerFontFamily(
        'CourierNew',
        normal='CourierNew',
        bold='CourierNew-Bold',
        italic='CourierNew-Italic',
        boldItalic='CourierNew-Bold'
    )

setup_fonts()

# ==============================================================================
# 2. NUMBERED CANVAS WITH RUNNING HEADER & FOOTER
# ==============================================================================
class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        # Suppress running header on page 1 (cover / header banner)
        if self._pageNumber > 1:
            self.setFont('Arial-Bold', 7.5)
            self.setFillColor(colors.HexColor('#475569'))
            self.drawString(36, 810, "JOB AUTOMATION SYSTEM — MASTER TECHNICAL MANUAL & OPERATING GUIDE")
            self.setFont('Arial', 7.5)
            self.drawRightString(559, 810, "AMRIT KUMAR • FULL STACK DEVELOPER")
            self.setStrokeColor(colors.HexColor('#CBD5E1'))
            self.setLineWidth(0.5)
            self.line(36, 804, 559, 804)

        # Footer on all pages
        self.setStrokeColor(colors.HexColor('#E2E8F0'))
        self.setLineWidth(0.5)
        self.line(36, 34, 559, 34)
        
        self.setFont('Arial', 7.5)
        self.setFillColor(colors.HexColor('#64748B'))
        self.drawString(36, 22, "Job Automation System v2.0 • Autonomous Multi-Portal Pipeline • Confidential")
        self.setFont('Arial-Bold', 7.5)
        self.drawRightString(559, 22, f"Page {self._pageNumber} of {page_count}")
        self.restoreState()

# ==============================================================================
# 3. HIGH-RES ARCHITECTURE DIAGRAM
# ==============================================================================
def ensure_architecture_diagram(output_path="assets/architecture_diagram.png"):
    if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
        return output_path
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    W, H = 2400, 1500
    img = Image.new("RGB", (W, H), "#0B0F19")
    draw = ImageDraw.Draw(img)

    try:
        font_header = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 44)
        font_sub = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
        font_col_title = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 26)
        font_card_title = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 20)
        font_card_desc = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
        font_badge = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 13)
        font_tag = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 13)
    except Exception:
        font_header = font_sub = font_col_title = font_card_title = font_card_desc = font_badge = font_tag = ImageFont.load_default()

    draw.rectangle([(0, 0), (W, 110)], fill="#111827")
    draw.line([(0, 110), (W, 110)], fill="#1E293B", width=2)
    draw.text((50, 24), "Job Automation System — Autonomous Architecture Overview", font=font_header, fill="#38BDF8")
    draw.text((50, 75), "Candidate: Amrit Kumar (Java 21, Spring Boot 3, React 19, Python, AWS)  •  8 Portals  •  100% Background Execution", font=font_sub, fill="#94A3B8")

    cols = [
        ("1. INGESTION & DATA FEEDS", 50, 500, "#38BDF8", "#0369A1"),
        ("2. CORE AUTOMATION ENGINES", 580, 1140, "#A855F7", "#7E22CE"),
        ("3. ORCHESTRATION & INTELLIGENCE", 1750, 600, "#10B981", "#047857"),
    ]

    for title, x, w, col_acc, col_border in cols:
        draw.rounded_rectangle([(x, 130), (x + w, H - 40)], radius=16, fill="#131B2E", outline=col_border, width=2)
        draw.rectangle([(x + 2, 132), (x + w - 2, 185)], fill="#1E293B")
        draw.text((x + 20, 145), title, font=font_col_title, fill=col_acc)

    def draw_card(x, y, w, h, title, subtitle, items, badge_text="", badge_color="#3B82F6"):
        draw.rounded_rectangle([(x, y), (x + w, y + h)], radius=12, fill="#1E293B", outline="#334155", width=2)
        draw.text((x + 16, y + 12), title, font=font_card_title, fill="#F8FAFC")
        if badge_text:
            bw = draw.textlength(badge_text, font=font_badge) + 16
            draw.rounded_rectangle([(x + w - bw - 14, y + 10), (x + w - 14, y + 32)], radius=6, fill=badge_color)
            draw.text((x + w - bw - 6, y + 14), badge_text, font=font_badge, fill="#FFFFFF")
        if subtitle:
            draw.text((x + 16, y + 36), subtitle, font=font_tag, fill="#94A3B8")
        cy = y + (54 if subtitle else 38)
        for it in items:
            draw.text((x + 16, cy), f"• {it}", font=font_card_desc, fill="#CBD5E1")
            cy += 20

    # Column 1 Cards (Inputs)
    draw_card(70, 205, 460, 120, "Dropzone Ingestion", "Filesystem & Cloud Storage", [
        "Google Drive Job_Dropzone (macOS CloudStorage)",
        "Local dropzone/ folder watcher",
        "Supports screenshots (.png, .jpg), .txt, email lists"
    ], "CLOUD + LOCAL", "#2563EB")

    draw_card(70, 345, 460, 140, "Primary Portal Feeds", "Daily Candidate Search Results", [
        "Naukri.com Search & Recommended Feeds",
        "Instahyre Candidate Opportunity Streams",
        "LinkedIn Jobs (Easy Apply filtered past 24h)",
        "LinkedIn Recruiter Posts (Past 24h live feed)"
    ], "TOP TIER", "#0EA5E9")

    draw_card(70, 505, 460, 160, "Broad Market Feeds", "Multi-portal aggregator feeds", [
        "Indeed India (in.indeed.com tech search)",
        "Shine.com (India tech hub queries)",
        "Wellfound (AngelList high-growth startups)",
        "Jobgether (Global remote & flexible tech)",
        "Foundit.in (Monster India tech opportunities)"
    ], "MULTI-PORTAL", "#6366F1")

    draw_card(70, 685, 460, 130, "Target Keywords & Stacks", "Filtered criteria from config.json", [
        "Java 21, Spring Boot 3, Microservices",
        "React 19, Python FastAPI, PostgreSQL, AWS",
        "Target Seniority: 3.5 YOE (skips > 5 YOE)",
        "Excludes: .NET, PHP, WordPress, Salesforce, QA"
    ], "STACK FILTER", "#8B5CF6")

    # Column 2 Left Sub-column
    draw_card(600, 205, 530, 175, "Cold Email & Dropzone Engine", "agent.py & cold_email_agent.py", [
        "Ollama LLM (llama3.1:8b) + Tesseract OCR extraction",
        "Parses Recruiter Name, Email, Company, Requirements",
        "Generates tailored LaTeX Resume & Cover Letter PDFs",
        "Dual-port Gmail SMTP (STARTTLS 587 + SSL 465 fallback)",
        "Anti-Spam: 14-day recipient cooldown, 5/company/day"
    ], "AI + LaTeX", "#EC4899")

    draw_card(600, 400, 530, 155, "Naukri.com Automation Engine", "naukri_agent.py", [
        "Profile Heartbeat: Daily Master PDF upload + headline dot toggle",
        "1-Click Apply on matching jobs (100 daily limit)",
        "Chatbot QA Solver: Answers CTC, notice period, skill YOE",
        "HR Email Bridge: Extracts recruiter emails to Dropzone"
    ], "100 / DAY", "#3B82F6")

    draw_card(600, 575, 530, 135, "Instahyre Automation Engine", "instahyre_agent.py", [
        "Profile Refresh: Daily Master PDF upload for freshness",
        "Modal-Scoped 1-Click Apply (100 daily limit)",
        "Pre-populated customized pitch note injection",
        "JS scrollIntoView & robust modal event dispatcher"
    ], "100 / DAY", "#14B8A6")

    draw_card(600, 730, 530, 195, "LinkedIn Automation Suite", "linkedin_agent.py (3 Engines)", [
        "Engine 1: 24h Recruiter Post Harvester (de-obfuscator, flyer OCR)",
        "Engine 2: 1-Click Easy Apply solver (20 daily limit)",
        "Engine 3: Hiring Manager & Agency Outreach (12/day, 80/wk)",
        "Preload direct custom-invite endpoint bypass",
        "Safety Auto-Lock on weekly limit & hard 30k connection guard",
        "Casual networking templates under 180 chars"
    ], "TRIPLE ENGINE", "#0284C7")

    # Column 2 Right Sub-column
    draw_card(1150, 205, 550, 255, "Multi-Portal Applier Suite", "indeed, shine, wellfound, jobgether, foundit", [
        "Indeed India (indeed_agent.py): 50 applies/day + QA solver",
        "Shine.com (shine_agent.py): 50 applies/day + profile refresh",
        "Wellfound Startups (wellfound_agent.py): 40 applies/day + pitch",
        "Jobgether Remote (jobgether_agent.py): 50 remote applies/day",
        "Foundit.in (foundit_agent.py): 50 applies/day + profile refresh",
        "All agents feature persistent Chrome profiles & HR email bridge",
        "Automatic stale Chrome lock file cleanup",
        "Combined Multi-Portal Daily Capacity: 240+ applications/day"
    ], "240+ APPLIES/DAY", "#F59E0B")

    draw_card(1150, 480, 550, 175, "On-Demand Resume Editor & PDF Engine", "resume_editor.py & resume_engine.py", [
        "ReportLab guaranteed 1-page ATS PDF generation",
        "Centralized master_resume.json data store",
        "Dynamic typography budgeting (zero 2nd-page spillover)",
        "Instant recruiter tailoring (CLI wizard, one-liners, Web UI)",
        "Auto-open in macOS Preview & Finder for instant dispatch"
    ], "ATS 1-PAGE", "#10B981")

    draw_card(1150, 675, 550, 250, "Autonomous Intelligence & Optimization", "optimizer.py & intent_miner.py", [
        "Screenshot Intelligence: Job DNA extraction from image drops",
        "Dynamic User Intent Profile with 21-day half-life decay",
        "Multi-Armed Bandit (MAB) with Thompson Sampling queries",
        "Automated Naukri headline skill injection magnetism",
        "No-Email Screenshot Lead Hunt queue routing to LinkedIn",
        "Continuous market vocabulary indexing in logs/",
        "Calibrated 0-100 Lead Quality Scoring Gate"
    ], "SELF-LEARNING", "#8B5CF6")

    # Column 3 Cards (Orchestration)
    draw_card(1770, 205, 560, 195, "Unified Command Center", "run_all.py", [
        "--status: Live dashboard of all portals, refreshes, emails",
        "--apply-all: Runs all 8 portals silently in sequence",
        "--refresh-all: Daily profile freshness across 6 portals",
        "--harvest-linkedin: Mine recruiter posts from past 24h",
        "--cold-email: Dispatch queued cold outreach leads",
        "--optimize: Executes autonomous self-learning audit"
    ], "CLI CONTROL", "#10B981")

    draw_card(1770, 420, 560, 175, "macOS Background Daemons", "launchd native agents", [
        "com.amrit.jobautomation.daily: Twice-daily multi-portal cycle",
        "Triggers at 09:00 AM & 03:00 PM IST every day",
        "com.amrit.jobautomation: 5-minute dropzone email watcher",
        "Runs silently without opening terminal windows",
        "Error-isolated phases: portal failures never block others"
    ], "NATIVE LAUNCHD", "#059669")

    draw_card(1770, 615, 560, 140, "Screen & Power Management", "power_utils.py (caffeinate -disu)", [
        "Spawns native macOS caffeinate -disu during active runs",
        "Keeps Mac and display ON to prevent network or sleep halts",
        "Restores default power-saving immediately upon completion",
        "Runs 100% headless or off-screen (zero desktop disruption)"
    ], "POWER MGMT", "#D97706")

    draw_card(1770, 775, 560, 150, "State & Persistence Ledgers", "logs/ directory", [
        "Full application tracking across all portals (JSON ledgers)",
        "LinkedIn connection tracking with weekly cap cooldowns",
        "Recipient deduplication & suppression lists",
        "SYSTEM_INTELLIGENCE.md: Evolving executive report"
    ], "PERSISTENCE", "#64748B")

    draw.rectangle([(50, H - 180), (W - 50, H - 60)], fill="#1E293B", outline="#3B82F6", width=2)
    stat_items = [
        ("460+", "Max Daily Portal Applies"),
        ("100+", "LinkedIn Recruiter Posts Mined/Day"),
        ("12/day", "Safe LinkedIn Outreaches (80/wk)"),
        ("8 Portals", "Autonomous Coverage"),
        ("100%", "Background macOS Native Execution"),
        ("1-Page ATS", "Dynamic ReportLab PDF Engine")
    ]
    sx = 80
    step = (W - 160) // len(stat_items)
    for val, lbl in stat_items:
        draw.text((sx + 10, H - 165), val, font=font_header, fill="#38BDF8")
        draw.text((sx + 10, H - 115), lbl, font=font_tag, fill="#CBD5E1")
        sx += step

    img.save(output_path, quality=95)
    return output_path

# ==============================================================================
# 4. MARKDOWN & CODE HELPERS
# ==============================================================================
def clean_markdown_inline(text):
    if not text:
        return ""
    text = re.sub(r'&(?!amp;|lt;|gt;|quot;|rarr;|ge;|le;|bull;)', '&amp;', text)
    text = text.replace(r'$\rightarrow$', '&rarr;')
    text = text.replace(r'$\ge$', '&ge;')
    text = text.replace(r'$\le$', '&le;')
    text = re.sub(r'\*\*\*(.*?)\*\*\*', r'<b><i>\1</i></b>', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^\*\n]+?)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'<font color="#2563EB"><u>\1</u></font>', text)
    def code_repl(m):
        raw = m.group(1).replace('<', '&lt;').replace('>', '&gt;')
        return f'<font face="CourierNew" color="#0F172A"><b>{raw}</b></font>'
    text = re.sub(r'`([^`]+)`', code_repl, text)
    return text

def wrap_code_smart(code_str, max_width=86):
    lines = []
    for line in code_str.splitlines():
        if len(line) <= max_width:
            lines.append(line)
        else:
            indent = len(line) - len(line.lstrip(' '))
            sub_indent = ' ' * (indent + 4)
            wrapped = textwrap.wrap(line, width=max_width, subsequent_indent=sub_indent)
            lines.extend(wrapped)
    return '\n'.join(lines)

def make_preformatted_code_box(code_text, title="", font_size=6.6, leading=8.4):
    wrapped = wrap_code_smart(code_text.strip('\n'))
    style = ParagraphStyle(
        f'CodeStyle_{font_size}_{leading}',
        fontName='CourierNew',
        fontSize=font_size,
        leading=leading,
        textColor=colors.HexColor('#0F172A')
    )
    lines = wrapped.splitlines()
    chunk_size = 35
    rows = []
    
    if title:
        header_style = ParagraphStyle(
            'CodeHeader',
            fontName='Arial-Bold',
            fontSize=7.5,
            leading=10,
            textColor=colors.HexColor('#334155')
        )
        title_p = Paragraph(f"<b>{title}</b>", header_style)
    else:
        title_p = None

    if len(lines) <= chunk_size:
        content = []
        if title_p:
            content.append(title_p)
            content.append(Spacer(1, 4))
        content.append(Preformatted(wrapped, style))
        rows.append([content])
    else:
        for idx, i in enumerate(range(0, len(lines), chunk_size)):
            chunk_lines = lines[i:i + chunk_size]
            content = []
            if idx == 0 and title_p:
                content.append(title_p)
                content.append(Spacer(1, 4))
            content.append(Preformatted('\n'.join(chunk_lines), style))
            rows.append([content])

    box = Table(rows, colWidths=[523])
    box.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
        ('BOX', (0,0), (-1,-1), 0.75, colors.HexColor('#CBD5E1')),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
    return box

# ==============================================================================
# 5. BUILD PDF DOCUMENT
# ==============================================================================
def build_manual_pdf(manual_path="SYSTEM_MANUAL.md", output_pdf="SYSTEM_MANUAL.pdf"):
    with open(manual_path, 'r', encoding='utf-8') as f:
        raw_text = f.read()

    doc = SimpleDocTemplate(
        output_pdf,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=44,
        bottomMargin=44
    )

    style_h1 = ParagraphStyle(
        'SecH1',
        fontName='Arial-Bold',
        fontSize=12.5,
        leading=15.5,
        textColor=colors.HexColor('#0F172A'),
        spaceBefore=12,
        spaceAfter=5,
        keepWithNext=True
    )
    
    style_h2 = ParagraphStyle(
        'SecH2',
        fontName='Arial-Bold',
        fontSize=9.5,
        leading=12.5,
        textColor=colors.HexColor('#1E3A8A'),
        spaceBefore=8,
        spaceAfter=3,
        keepWithNext=True
    )

    style_body = ParagraphStyle(
        'Body',
        fontName='Arial',
        fontSize=7.8,
        leading=11,
        textColor=colors.HexColor('#1E293B'),
        spaceBefore=1,
        spaceAfter=3
    )

    style_bullet1 = ParagraphStyle(
        'Bullet1',
        fontName='Arial',
        fontSize=7.8,
        leading=10.8,
        textColor=colors.HexColor('#1E293B'),
        leftIndent=12,
        firstLineIndent=-8,
        spaceBefore=1,
        spaceAfter=2
    )

    style_bullet2 = ParagraphStyle(
        'Bullet2',
        fontName='Arial',
        fontSize=7.3,
        leading=10.2,
        textColor=colors.HexColor('#334155'),
        leftIndent=24,
        firstLineIndent=-8,
        spaceBefore=1,
        spaceAfter=1
    )

    style_td = ParagraphStyle(
        'TableCell',
        fontName='Arial',
        fontSize=7,
        leading=9,
        textColor=colors.HexColor('#1E293B')
    )

    story = []

    # ==========================================================================
    # PAGE 1: TITLE, EXECUTIVE OVERVIEW & CANDIDATE PROFILE SPEC
    # ==========================================================================
    banner_content = [
        Paragraph(
            "<font color='#38BDF8'><b>AUTONOMOUS MULTI-PORTAL JOB APPLICATION ENGINE</b></font> &nbsp;|&nbsp; "
            "<font color='#94A3B8'>MASTER OPERATING MANUAL</font>",
            ParagraphStyle('TopMeta', fontName='Arial-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#38BDF8'))
        ),
        Spacer(1, 6),
        Paragraph(
            "<b>Job Automation System — Master Technical Manual &amp; Operating Guide</b>",
            ParagraphStyle('BannerTitle', fontName='Arial-Bold', fontSize=18, leading=22, textColor=colors.white)
        ),
        Spacer(1, 6),
        Paragraph(
            "A centralized, autonomous job hunting, profiling, and application engine tailored for <b>Amrit Kumar</b><br/>"
            "<font color='#CBD5E1'>Full Stack Developer: Java 21, Spring Boot 3, Microservices, React 19, Python FastAPI, PostgreSQL, AWS</font>",
            ParagraphStyle('BannerSub', fontName='Arial', fontSize=8.5, leading=12.5, textColor=colors.HexColor('#E2E8F0'))
        ),
        Spacer(1, 8),
        Paragraph(
            "<font color='#38BDF8'><b>Core Portal Coverage:</b></font> Naukri.com &bull; Instahyre &bull; LinkedIn &bull; Indeed India &bull; Shine.com &bull; Wellfound &bull; Jobgether &bull; Foundit &bull; Cold Email OCR Pipeline<br/>"
            "<font color='#38BDF8'><b>Autonomous Architecture:</b></font> 100% Native macOS <font face='CourierNew'>launchd</font> Daemons &bull; Screen Keep-Awake (<font face='CourierNew'>caffeinate -disu</font>) &bull; Dynamic LaTeX ATS Resume Engine",
            ParagraphStyle('BannerTags', fontName='Arial', fontSize=7.2, leading=10.5, textColor=colors.HexColor('#94A3B8'))
        )
    ]
    banner_table = Table([[banner_content]], colWidths=[523])
    banner_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#0F172A')),
        ('BOX', (0,0), (-1,-1), 1.5, colors.HexColor('#1E3A8A')),
        ('LINEBEFORE', (0,0), (-1,-1), 5, colors.HexColor('#38BDF8')),
        ('TOPPADDING', (0,0), (-1,-1), 12),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
        ('LEFTPADDING', (0,0), (-1,-1), 14),
        ('RIGHTPADDING', (0,0), (-1,-1), 14),
    ]))
    story.append(banner_table)
    story.append(Spacer(1, 10))

    # Candidate Profile & System Metrics Card
    cand_col1 = [
        Paragraph("<b>CANDIDATE SPECIFICATION</b>", ParagraphStyle('H', fontName='Arial-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#1E3A8A'))),
        Spacer(1, 4),
        Paragraph("&bull; <b>Candidate:</b> Amrit Kumar (Bangalore, India)", style_td),
        Paragraph("&bull; <b>Primary Stack:</b> Java 21, Spring Boot 3, Microservices", style_td),
        Paragraph("&bull; <b>Secondary:</b> React 19, Python FastAPI, PostgreSQL, AWS", style_td),
        Paragraph("&bull; <b>Experience:</b> 3.5 Years Total (3.0 Years Relevant)", style_td),
        Paragraph("&bull; <b>Notice Period:</b> 7 Days (Immediate Joiner)", style_td),
        Paragraph("&bull; <b>Compensation:</b> Current 12 LPA &bull; Expected 15 LPA", style_td),
    ]
    cand_col2 = [
        Paragraph("<b>SYSTEM OPERATING METRICS</b>", ParagraphStyle('H', fontName='Arial-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#1E3A8A'))),
        Spacer(1, 4),
        Paragraph("&bull; <b>Daily Apply Capacity:</b> 460+ across 8 job portals", style_td),
        Paragraph("&bull; <b>LinkedIn Harvester:</b> 100+ Recruiter Posts Mined / day", style_td),
        Paragraph("&bull; <b>LinkedIn Outreach:</b> 12 invites/day (Safe 80/wk cap)", style_td),
        Paragraph("&bull; <b>Cold Email Cadence:</b> Every 5 mins via macOS daemon", style_td),
        Paragraph("&bull; <b>Multi-Portal Schedule:</b> 09:00 AM &amp; 03:00 PM IST daily", style_td),
        Paragraph("&bull; <b>Power Control:</b> <font face='CourierNew'>caffeinate -disu</font> keep-awake", style_td),
    ]
    spec_table = Table([[cand_col1, cand_col2]], colWidths=[261, 262])
    spec_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(spec_table)
    story.append(Spacer(1, 10))

    # Executive TOC Summary
    toc_data = [
        [
            Paragraph("<b>1. System Architecture Overview</b><br/><font color='#64748B'>Full multi-tier visual diagram, flows &amp; data bridges</font>", style_td),
            Paragraph("<b>2. Core Components &amp; Feature Breakdown</b><br/><font color='#64748B'>Detailed specifications for all 8 portal agents &amp; AI engines</font>", style_td),
        ],
        [
            Paragraph("<b>3. Operational Schedules &amp; Execution</b><br/><font color='#64748B'>macOS launchd daemons, limits, timing &amp; keep-awake</font>", style_td),
            Paragraph("<b>4. Complete CLI Command Reference</b><br/><font color='#64748B'>Dashboard, logins, refreshes, auto-applies &amp; editor</font>", style_td),
        ],
        [
            Paragraph("<b>5. Candidate Profile &amp; QA Bank Reference</b><br/><font color='#64748B'>Centralized screening answers, schema &amp; JSON configs</font>", style_td),
            Paragraph("<b>6. Directory Structure &amp; Diagnostics</b><br/><font color='#64748B'>Repository file tree &amp; comprehensive troubleshooting table</font>", style_td),
        ]
    ]
    toc_table = Table(toc_data, colWidths=[261, 262])
    toc_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#EFF6FF')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#BFDBFE')),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#DBEAFE')),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(toc_table)

    # ==========================================================================
    # PAGE 2: SECTION 1 - SYSTEM ARCHITECTURE OVERVIEW
    # ==========================================================================
    story.append(PageBreak())
    story.append(Paragraph("<b>1. System Architecture Overview</b>", style_h1))
    story.append(Paragraph(
        "The system is organized into three decoupled, fault-tolerant tiers: <b>(1) Multi-Source Ingestion &amp; Data Feeds</b>, "
        "<b>(2) Core Autonomous Engines &amp; AI Document Synthesis</b>, and <b>(3) Unified Orchestrator &amp; Background Schedulers</b>. "
        "The diagram below illustrates complete data flows, OCR pipelines, AI extraction, and portal execution bridges.",
        style_body
    ))
    story.append(Spacer(1, 4))

    diag_path = ensure_architecture_diagram()
    if os.path.exists(diag_path):
        img_w = 523
        img_h = 523 * (1500 / 2400) # ~ 326 pt
        story.append(RLImage(diag_path, width=img_w, height=img_h))
        story.append(Spacer(1, 6))

    callout_content = [
        Paragraph("<b>ARCHITECTURE HIGHLIGHTS &amp; DATA FLOW PATTERNS</b>", ParagraphStyle('CallTitle', fontName='Arial-Bold', fontSize=7.5, leading=9.5, textColor=colors.HexColor('#1E40AF'))),
        Spacer(1, 3),
        Paragraph("&bull; <b>Universal Dropzone Hub:</b> All external recruiter contacts, flyer screenshots, and harvested LinkedIn posts route into the Dropzone (Google Drive or local), creating a single consolidated entry point.", style_body),
        Paragraph("&bull; <b>Decoupled Fault Isolation:</b> Each portal agent (Naukri, Instahyre, LinkedIn, Indeed, Shine, Wellfound, Jobgether, Foundit) executes in its own sandboxed context with distinct Chrome profiles, preventing cascade failures.", style_body),
        Paragraph("&bull; <b>Dual-Phase Daily Execution:</b> High-frequency jobs (Dropzone email watcher) trigger every 5 minutes, while deep portal application passes run twice daily at 09:00 AM &amp; 03:00 PM IST with system keep-awake.", style_body),
    ]
    callout_box = Table([[callout_content]], colWidths=[523])
    callout_box.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#EFF6FF')),
        ('LINEBEFORE', (0,0), (-1,-1), 3.5, colors.HexColor('#2563EB')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#BFDBFE')),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(callout_box)

    # ==========================================================================
    # SECTION 2: CORE COMPONENTS & FEATURE BREAKDOWN
    # ==========================================================================
    story.append(PageBreak())
    story.append(Paragraph("<b>2. Core Components &amp; Feature Breakdown</b>", style_h1))
    story.append(Paragraph(
        "The Job Automation engine comprises specialized agent modules designed for targeted tasks: dropzone file watching, "
        "multimodal OCR, speculative cold email copywriting, multi-portal browser automation, and continuous query optimization.",
        style_body
    ))
    story.append(Spacer(1, 4))

    parts = re.split(r'\n(?=## \d+\. )', raw_text)
    subparts = re.split(r'\n(?=### [A-Z](?:\.\d+)?\.\s+)', parts[2])
    for sp in subparts:
        sp = sp.strip()
        if not sp or sp.startswith("## 2."):
            continue
        
        lines = sp.splitlines()
        header_line = lines[0].replace('### ', '').strip()
        clean_header = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', header_line)
        story.append(Paragraph(f"<b>{clean_markdown_inline(header_line)}</b>", style_h2))
        
        for l in lines[1:]:
            l_str = l.strip()
            if not l_str:
                continue
            if l_str == '---':
                story.append(Spacer(1, 3))
                continue
            
            if l.startswith("    - ") or l.startswith("  - ") or l.startswith("    * "):
                bullet_text = clean_markdown_inline(l_str.lstrip('-* \t'))
                story.append(Paragraph(f"&ndash;&nbsp; {bullet_text}", style_bullet2))
            elif l.startswith("- ") or l.startswith("* "):
                bullet_text = clean_markdown_inline(l_str[2:].strip())
                story.append(Paragraph(f"&bull;&nbsp; {bullet_text}", style_bullet1))
            elif re.match(r'^\d+\.\s+', l_str):
                num = re.match(r'^(\d+\.)\s+', l_str).group(1)
                num_text = clean_markdown_inline(l_str[len(num):].strip())
                story.append(Paragraph(f"<b>{num}</b>&nbsp; {num_text}", style_bullet1))
            elif l_str.startswith(">"):
                quote_text = clean_markdown_inline(l_str.lstrip('> \t'))
                story.append(Paragraph(f"<i>&ldquo;{quote_text}&rdquo;</i>", style_bullet2))
            else:
                story.append(Paragraph(clean_markdown_inline(l_str), style_body))
        
        story.append(Spacer(1, 4))

    # ==========================================================================
    # SECTION 3: OPERATIONAL SCHEDULES & AUTONOMOUS EXECUTION
    # ==========================================================================
    story.append(PageBreak())
    story.append(Paragraph("<b>3. Operational Schedules &amp; Autonomous Execution</b>", style_h1))
    story.append(Paragraph(
        "The system is configured to run <b>100% autonomously in the background every day</b> using native macOS <font face='CourierNew'><b>launchd</b></font> agents. "
        "No active terminal session or user intervention is required during scheduled cycles.",
        style_body
    ))
    story.append(Spacer(1, 4))

    th_sched = ParagraphStyle('THS', fontName='Arial-Bold', fontSize=7.5, leading=9.5, textColor=colors.white)
    td_sched = ParagraphStyle('TDS', fontName='Arial', fontSize=6.6, leading=8.6, textColor=colors.HexColor('#0F172A'))
    td_sched_bold = ParagraphStyle('TDSB', fontName='Arial-Bold', fontSize=6.6, leading=8.6, textColor=colors.HexColor('#1E3A8A'))

    sched_headers = [
        Paragraph("<b>Pipeline / Daemon</b>", th_sched),
        Paragraph("<b>Trigger Schedule (IST)</b>", th_sched),
        Paragraph("<b>Daily Limit &amp; Quotas</b>", th_sched),
        Paragraph("<b>Execution Mode</b>", th_sched),
        Paragraph("<b>Autonomous Pipeline Steps</b>", th_sched)
    ]

    row1 = [
        Paragraph("<b>com.amrit.jobautomation.daily</b><br/><font color='#64748B'><i>(Twice-Daily Multi-Portal Cycle)</i></font>", td_sched_bold),
        Paragraph("<b>Twice Daily:</b><br/>09:00 AM &amp;<br/>03:00 PM IST", td_sched),
        Paragraph("<b>460 total applies / day</b><br/>(100 Naukri + 100 Instahyre + 50 Indeed + 50 Shine + 50 Jobgether + 50 Foundit + 40 Wellfound + 20 LinkedIn Easy Apply)<br/><b>+ 100 Recruiter Posts</b><br/><b>+ 12 Outreaches / day</b><br/>(80/wk Safe Cap)", td_sched),
        Paragraph("Silent Background<br/>(<font face='CourierNew'>caffeinate -disu</font> + Headless Chrome)", td_sched),
        Paragraph(
            "1. Spawns <b>caffeinate -disu</b> keeping Mac and screen awake.<br/>"
            "2. Compiles freshly dated Master Resume PDF.<br/>"
            "3. Uploads resume &amp; toggles headline heartbeat on <b>Naukri</b>.<br/>"
            "4. Uploads fresh resume to <b>Instahyre</b> to boost recruiter freshness.<br/>"
            "5. Refreshes candidate profiles on <b>Shine.com</b>, <b>Wellfound</b>, <b>Jobgether</b>, and <b>Foundit</b>.<br/>"
            "6. Auto-applies matching target stack on <b>Naukri</b> (max 100/day).<br/>"
            "7. Auto-applies matching opportunities on <b>Instahyre</b> (max 100/day).<br/>"
            "8. Mines up to <b>50 LinkedIn recruiter posts</b> per run (max 100/day) in past 24h &amp; drops tickets to Dropzone.<br/>"
            "9. Auto-applies on <b>Indeed India</b> (max 50/day), <b>Shine.com</b> (max 50/day), <b>Jobgether</b> (max 50/day), <b>Foundit</b> (max 50/day), and <b>Wellfound</b> (max 40/day).<br/>"
            "10. Auto-applies up to <b>20 LinkedIn Easy Apply</b> jobs.<br/>"
            "11. Sends up to <b>12 LinkedIn connection requests</b> to verified hiring managers &amp; recruiters.<br/>"
            "12. Sends a single final summary notification and restores standard power saving.",
            td_sched
        )
    ]

    row2 = [
        Paragraph("<b>com.amrit.jobautomation</b><br/><font color='#64748B'><i>(Dropzone &amp; Email Watcher)</i></font>", td_sched_bold),
        Paragraph("<b>Every 5 Minutes</b><br/>(Active 10:00 AM &ndash; 06:00 PM IST)", td_sched),
        Paragraph("<b>Uncapped</b><br/>(Respects 14-day recipient cooldown &amp; 5/domain/day cap)", td_sched),
        Paragraph("Native launchd<br/>Interval Job<br/>(<font face='CourierNew'>StartInterval: 300</font>)", td_sched),
        Paragraph(
            "1. Scans Google Drive <b>Job_Dropzone</b> and local <b>dropzone/</b>.<br/>"
            "2. OCRs screenshots / reads text files and email dumps.<br/>"
            "3. Extracts recruiter emails and JD requirements with local Ollama.<br/>"
            "4. Renders customized LaTeX Resume &amp; Cover Letter PDFs.<br/>"
            "5. Dispatches tailored application emails via Gmail SMTP (STARTTLS 587 + SSL 465 fallback).",
            td_sched
        )
    ]

    sched_table = Table([sched_headers, row1, row2], colWidths=[90, 72, 88, 85, 188])
    sched_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0F172A')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BACKGROUND', (0,1), (-1,1), colors.white),
        ('BACKGROUND', (0,2), (-1,2), colors.HexColor('#F8FAFC')),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(sched_table)
    story.append(Spacer(1, 8))

    sched_callout = [
        Paragraph("<b>CRITICAL OPERATIONAL &amp; SAFETY PRINCIPLES</b>", ParagraphStyle('CP', fontName='Arial-Bold', fontSize=7.5, leading=9.5, textColor=colors.HexColor('#92400E'))),
        Spacer(1, 3),
        Paragraph("&bull; <b>Silent Background Multitasking:</b> Runs 100% headless in the background without popping up Chrome windows or interrupting active desktop work.", style_body),
        Paragraph("&bull; <b>Screen &amp; System Keep-Awake (<font face='CourierNew'>caffeinate -disu</font>):</b> During active automation, Mac and display remain awake to ensure network interfaces and background sockets do not freeze. Power saving restores immediately after completion.", style_body),
        Paragraph("&bull; <b>Quiet Mode:</b> Routine individual job application popups are silenced (<font face='CourierNew'>quiet_mode: true</font>), delivering only a single consolidated summary notification.", style_body),
        Paragraph("&bull; <b>Fault-Tolerant Execution:</b> Every phase (Naukri, Instahyre, LinkedIn, Indeed, Shine, Wellfound, Jobgether, Foundit, Cold Email) operates with complete error containment — an issue or timeout in one portal never blocks others.", style_body),
        Paragraph("&bull; <b>Anti-Bot Safety Caps:</b> Conservative daily limits (100 Naukri, 100 Instahyre, 50 Indeed/Shine/Jobgether/Foundit, 40 Wellfound, 20 LinkedIn Apply, 12 LinkedIn Outreach) with human-like delays (1.5s–4.5s; 4.0s–7.5s for outreach) protect account reputation.", style_body),
    ]
    sched_box = Table([[sched_callout]], colWidths=[523])
    sched_box.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#FFFBEB')),
        ('LINEBEFORE', (0,0), (-1,-1), 3.5, colors.HexColor('#F59E0B')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#FDE68A')),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(sched_box)

    # ==========================================================================
    # SECTION 4: COMPLETE CLI COMMAND REFERENCE
    # ==========================================================================
    story.append(PageBreak())
    story.append(Paragraph("<b>4. Complete CLI Command Reference</b>", style_h1))
    story.append(Paragraph(
        "Always run commands from the project root directory: <font face='CourierNew'><b>cd ~/Documents/JobAutomation</b></font>. "
        "Below is the complete reference of management commands, status queries, on-demand application triggers, and daemon inspection commands.",
        style_body
    ))
    story.append(Spacer(1, 4))

    cli_subsections = re.split(r'\n(?=### )', parts[4])
    for csub in cli_subsections:
        csub = csub.strip()
        if not csub or csub.startswith("## 4."):
            continue
        
        sub_items = []
        lines = csub.splitlines()
        c_title = lines[0].replace('### ', '').strip()
        sub_items.append(Paragraph(f"<b>{c_title}</b>", style_h2))

        sub_text = '\n'.join(lines[1:])
        code_matches = re.findall(r'```(?:bash)?\n(.*?)\n```', sub_text, re.DOTALL)
        non_code = re.sub(r'```(?:bash)?\n.*?\n```', '', sub_text, flags=re.DOTALL).strip()
        
        if non_code:
            for nl in non_code.splitlines():
                if nl.strip():
                    sub_items.append(Paragraph(clean_markdown_inline(nl.strip()), style_body))
            sub_items.append(Spacer(1, 1))

        for cb in code_matches:
            sub_items.append(make_preformatted_code_box(cb, font_size=6.2, leading=7.7))
            sub_items.append(Spacer(1, 2))
        
        sub_items.append(Spacer(1, 2))
        story.append(KeepTogether(sub_items))

    # ==========================================================================
    # SECTION 5: CANDIDATE PROFILE & QA BANK REFERENCE
    # ==========================================================================
    story.append(PageBreak())
    story.append(Paragraph("<b>5. Candidate Profile &amp; QA Bank Reference</b>", style_h1))
    story.append(Paragraph(
        "The candidate profile configuration in <font face='CourierNew'><b>config.json</b></font> acts as the single source of truth across all automation engines. "
        "Screening questions, notice periods, salary figures, outreach templates, and search keywords are managed centrally here:",
        style_body
    ))
    story.append(Spacer(1, 4))

    json_match = re.search(r'```json\n(.*?)\n```', parts[5], re.DOTALL)
    if json_match:
        story.append(make_preformatted_code_box(json_match.group(1), title="config.json (Single Source of Truth Configuration)", font_size=5.7, leading=7.0))
    else:
        story.append(Paragraph(clean_markdown_inline(parts[5].strip()), style_body))

    # ==========================================================================
    # SECTION 6: DIRECTORY STRUCTURE & KEY FILES
    # ==========================================================================
    story.append(PageBreak())
    story.append(Paragraph("<b>6. Directory Structure &amp; Key Files</b>", style_h1))
    story.append(Paragraph(
        "The repository is organized cleanly with explicit separation between engine scripts, persistent browser profiles, "
        "runtime audit ledgers, LaTeX document templates, and the dropzone ingestion pipeline:",
        style_body
    ))
    story.append(Spacer(1, 4))

    tree_match = re.search(r'```(?:text)?\n(.*?)\n```', parts[6], re.DOTALL)
    if tree_match:
        story.append(make_preformatted_code_box(tree_match.group(1), title="Repository Architecture (./JobAutomation/)", font_size=6.0, leading=7.5))
    else:
        story.append(Paragraph(clean_markdown_inline(parts[6].strip()), style_body))
    story.append(Spacer(1, 8))

    # ==========================================================================
    # SECTION 7: TROUBLESHOOTING & DIAGNOSTICS
    # ==========================================================================
    story.append(Paragraph("<b>7. Troubleshooting &amp; Diagnostics</b>", style_h1))
    story.append(Paragraph(
        "Quick diagnostic guide for common operating hurdles, stale browser lock files, Cloudflare challenges, and networking timeouts:",
        style_body
    ))
    story.append(Spacer(1, 4))

    th_diag = ParagraphStyle('THD', fontName='Arial-Bold', fontSize=7.5, leading=9.5, textColor=colors.white)
    td_diag_bold = ParagraphStyle('TDDB', fontName='CourierNew-Bold', fontSize=6.6, leading=8.6, textColor=colors.HexColor('#1E3A8A'))
    td_diag = ParagraphStyle('TDD', fontName='Arial', fontSize=6.6, leading=8.6, textColor=colors.HexColor('#0F172A'))

    diag_headers = [
        Paragraph("<b>Issue / Error Condition</b>", th_diag),
        Paragraph("<b>Root Cause</b>", th_diag),
        Paragraph("<b>Recommended Solution &amp; Fix</b>", th_diag)
    ]

    diag_rows = [
        [
            Paragraph("<b>ProcessSingleton: File exists (SingletonLock)</b>", td_diag_bold),
            Paragraph("Chrome was closed abruptly or crashed leaving a stale lock file in the browser profile directory.", td_diag),
            Paragraph("The automation scripts auto-clean stale locks on launch. Alternatively run:<br/><font face='CourierNew'><b>python3 -c \"import naukri_agent; naukri_agent.cleanup_stale_chrome_locks('browser_profile/instahyre')\"</b></font>", td_diag)
        ],
        [
            Paragraph("<b>Cloudflare Challenge block</b>", td_diag_bold),
            Paragraph("Running in headless mode during Cloudflare interactive bot inspection.", td_diag),
            Paragraph("Run with the <font face='CourierNew'><b>--headed</b></font> flag so the Chrome GUI opens with real user context, solve the check once, then resume silent headless mode.", td_diag)
        ],
        [
            Paragraph("<b>[Errno 2] No such file or directory</b>", td_diag_bold),
            Paragraph("Running the command from outside the project root directory.", td_diag),
            Paragraph("Always switch to project root first: <font face='CourierNew'><b>cd ~/Documents/JobAutomation</b></font> before executing agent commands.", td_diag)
        ],
        [
            Paragraph("<b>SMTP connection closed unexpectedly</b>", td_diag_bold),
            Paragraph("Google Gmail temporary socket timeout or rate spike.", td_diag),
            Paragraph("Automatic retry with fallback from Port 587 (STARTTLS) to Port 465 (SSL) is built-in. Files remain in dropzone for subsequent ticks.", td_diag)
        ],
        [
            Paragraph("<b>LinkedIn invitation modal click intercepted</b>", td_diag_bold),
            Paragraph("Sticky navigation header (<font face='CourierNew'>Claim 1 free month</font>) or Creator Mode layout blocking the modal button.", td_diag),
            Paragraph("The engine automatically routes to the native modal endpoint: <font face='CourierNew'><b>https://www.linkedin.com/preload/custom-invite/?vanityName=&lt;vanity&gt;</b></font> bypassing DOM layout hurdles.", td_diag)
        ],
        [
            Paragraph("<b>LinkedIn monthly personalized note limit reached</b>", td_diag_bold),
            Paragraph("Free LinkedIn account exhausted monthly custom note quota.", td_diag),
            Paragraph("The engine automatically detects the restriction and falls back to sending a clean standard invitation without failing (<font face='CourierNew'><b>send_without_note_if_quota_exhausted: true</b></font>).", td_diag)
        ],
    ]

    table_data = [diag_headers] + diag_rows
    diag_table = Table(table_data, colWidths=[125, 135, 263])
    
    table_style = [
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0F172A')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
    ]
    for i in range(1, len(table_data)):
        bg = colors.white if i % 2 == 1 else colors.HexColor('#F8FAFC')
        table_style.append(('BACKGROUND', (0, i), (-1, i), bg))
        
    diag_table.setStyle(TableStyle(table_style))
    story.append(diag_table)

    # Build the document
    print("Building refined PDF document...")
    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Base PDF built: {output_pdf}")

    # ==========================================================================
    # SET ACCURATE PYMUPDF TOC OUTLINES & METADATA
    # ==========================================================================
    print("Setting exact PDF bookmarks and document metadata...")
    pdf_doc = fitz.open(output_pdf)
    
    pdf_doc.set_metadata({
        "title": "Job Automation System — Master Technical Manual & Operating Guide",
        "author": "Amrit Kumar",
        "subject": "Autonomous Multi-Portal Job Application Engine Documentation",
        "keywords": "Job Automation, Python, Selenium, Playwright, ReportLab, LaTeX, Java, Spring Boot, React, Naukri, Instahyre, LinkedIn, Indeed, Shine, Wellfound, Jobgether, Foundit",
        "creator": "ReportLab PDF Engine & PyMuPDF",
        "producer": "Amrit Kumar Autonomous Engine"
    })

    exact_headings = [
        (1, '1. System Architecture Overview', 2),
        (1, '2. Core Components & Feature Breakdown', 3),
        (2, 'A. Cold Email & Dropzone Pipeline', 3),
        (2, 'A.1 Direct Cold Outreach Engine', 3),
        (2, 'B. Naukri.com Automation Engine', 3),
        (2, 'C. Instahyre Automation Engine', 4),
        (2, 'D. LinkedIn Automation Engine', 4),
        (2, 'E. Indeed India Automation Engine', 5),
        (2, 'F. Shine.com Automation Engine', 5),
        (2, 'G. Wellfound Startup Engine', 5),
        (2, 'H. Jobgether Remote & Flexible Work Engine', 6),
        (2, 'I. Foundit Automation Engine', 6),
        (2, 'J. Autonomous Self-Learning & Optimization', 6),
        (2, 'K. On-Demand Resume Editor & PDF Generator', 7),
        (2, 'L. Unified Command Center', 7),
        (1, '3. Operational Schedules & Autonomous Execution', 8),
        (1, '4. Complete CLI Command Reference', 9),
        (2, 'Dashboard & Status', 9),
        (2, 'Initial One-Time Authentication Setup', 9),
        (2, 'Daily Profile Refreshes', 9),
        (2, 'Automated Job Applications', 9),
        (2, 'LinkedIn 24h Recruiter Post Harvester', 9),
        (2, 'LinkedIn Outreach (Free-Account Safe)', 9),
        (2, 'Cold Email Dispatcher', 10),
        (2, 'Autonomous Self-Learning Cycle', 10),
        (2, 'On-Demand Resume Editor Commands', 10),
        (2, 'Full Daily Automation Cycle', 10),
        (2, 'Background Automation Daemons (launchd)', 10),
        (1, '5. Candidate Profile & QA Bank Reference', 11),
        (1, '6. Directory Structure & Key Files', 12),
        (1, '7. Troubleshooting & Diagnostics', 12),
    ]
    pdf_doc.set_toc(exact_headings)
    temp_fixed = output_pdf + ".tmp"
    pdf_doc.save(temp_fixed)
    pdf_doc.close()
    os.replace(temp_fixed, output_pdf)
    print(f"Final publication-grade PDF ready: {output_pdf} ({os.path.getsize(output_pdf)} bytes)")

if __name__ == "__main__":
    build_manual_pdf()
