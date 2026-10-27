#!/usr/bin/env python3
"""
Comprehensive Test Suite for Google Forms & Microsoft Forms Automation Engine
Tests:
  1. QA Inference & Answer Resolution across all standard recruiter question categories
  2. Google Forms DOM Solver on realistic mock Google Form
  3. Microsoft Forms DOM Solver on realistic mock Microsoft Form
  4. ATS Bridge detection, unshortening, and routing
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

import ats_bridge
import forms_agent
import agent as email_agent


class TestFormsEngine(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.config = email_agent.load_config()

    def test_01_ats_bridge_url_detection(self):
        """Verify ats_bridge accurately detects Google Forms and Microsoft Forms URLs."""
        test_cases = [
            ("Apply here: https://forms.gle/7XpL8kQ9m2n", "google_forms", "https://forms.gle/7XpL8kQ9m2n"),
            ("Google form link: https://docs.google.com/forms/d/e/1FAIpQLSc_xyz/viewform?usp=sf_link.", "google_forms", "https://docs.google.com/forms/d/e/1FAIpQLSc_xyz/viewform?usp=sf_link"),
            ("Microsoft forms: https://forms.office.com/r/abc123XYZ", "ms_forms", "https://forms.office.com/r/abc123XYZ"),
            ("MS Form: https://forms.microsoft.com/Pages/ResponsePage.aspx?id=v4j5cv", "ms_forms", "https://forms.microsoft.com/Pages/ResponsePage.aspx?id=v4j5cv"),
            ("Cloud MS Form: https://forms.cloud.microsoft/r/test99", "ms_forms", "https://forms.cloud.microsoft/r/test99"),
            ("Greenhouse link: https://boards.greenhouse.io/stripe/jobs/12345", "greenhouse", "https://boards.greenhouse.io/stripe/jobs/12345"),
            ("Workday link: https://adobe.wd5.myworkdayjobs.com/job/123", "workday", "https://adobe.wd5.myworkdayjobs.com/job/123"),
        ]

        for text, expected_type, expected_url in test_cases:
            ats_type, ats_url = ats_bridge.detect_direct_ats_url(text)
            self.assertEqual(ats_type, expected_type, f"Failed type for '{text}'")
            self.assertEqual(ats_url, expected_url, f"Failed URL for '{text}'")

    def test_02_qa_inference_personal_details(self):
        """Verify contact & personal question inference."""
        cfg = self.config
        self.assertEqual(forms_agent.resolve_form_question("Full Name", config=cfg), "Amrit Kumar")
        self.assertEqual(forms_agent.resolve_form_question("First Name", config=cfg), "Amrit")
        self.assertEqual(forms_agent.resolve_form_question("Last Name", config=cfg), "Kumar")
        self.assertEqual(forms_agent.resolve_form_question("Email Address", config=cfg), "candidate@example.com")
        self.assertIn("9876543210", forms_agent.resolve_form_question("Contact Number", config=cfg))
        self.assertEqual(forms_agent.resolve_form_question("Phone (10 digits only)", config=cfg), "9876543210")
        self.assertEqual(forms_agent.resolve_form_question("LinkedIn Profile URL", config=cfg), "https://linkedin.com/in/yourprofile")
        self.assertEqual(forms_agent.resolve_form_question("GitHub URL", config=cfg), "https://github.com/yourusername")

    def test_03_qa_inference_experience_and_ctc(self):
        """Verify professional, CTC, and notice period question resolution."""
        cfg = self.config
        # Experience
        tot_exp = forms_agent.resolve_form_question("Total Years of Experience", config=cfg)
        self.assertIn("3.5", str(tot_exp))

        # Notice period
        notice = forms_agent.resolve_form_question("What is your Notice Period?", config=cfg)
        self.assertTrue("7" in notice or "Immediate" in notice)

        notice_opt = forms_agent.resolve_form_question(
            "Notice Period",
            available_options=["Immediate to 15 days", "30 Days", "60 Days", "90 Days"],
            config=cfg
        )
        self.assertEqual(notice_opt, "Immediate to 15 days")

        # CTC
        c_ctc = forms_agent.resolve_form_question("Current CTC (in LPA)", config=cfg)
        self.assertIn("12", str(c_ctc))

        e_ctc = forms_agent.resolve_form_question("Expected CTC (in LPA)", config=cfg)
        self.assertIn("15", str(e_ctc))

        # Serving notice
        serving = forms_agent.resolve_form_question("Are you currently serving notice period?", available_options=["Yes", "No"], config=cfg)
        self.assertEqual(serving, "Yes")

        # Company & Designation
        comp = forms_agent.resolve_form_question("Current Company / Employer", config=cfg)
        self.assertEqual(comp, "Tech Solutions Inc.")
        role = forms_agent.resolve_form_question("Current Designation / Role", config=cfg)
        self.assertEqual(role, "Full Stack Developer")

    def test_04_qa_inference_checkbox_skills(self):
        """Verify multi-selection of candidate skills in checkbox fields."""
        cfg = self.config
        options = ["C# / .NET", "Java", "Spring Boot", "PHP", "React.js", "Ruby", "PostgreSQL", "WordPress"]
        selected = forms_agent.resolve_form_question(
            "Select all technologies you are proficient in:",
            field_type="checkbox",
            available_options=options,
            config=cfg
        )
        self.assertIsInstance(selected, list)
        self.assertIn("Java", selected)
        self.assertIn("Spring Boot", selected)
        self.assertIn("React.js", selected)
        self.assertIn("PostgreSQL", selected)
        self.assertNotIn("PHP", selected)
        self.assertNotIn("WordPress", selected)
        self.assertNotIn("C# / .NET", selected)

    def test_05_google_forms_dom_solver(self):
        """Test Google Forms solver on realistic mock Google Forms HTML."""
        mock_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Engineering Application</title></head>
        <body>
            <div class="F9NWFb">Full Stack Developer Application</div>
            <form action="/formResponse">
                <div role="list">
                    <!-- Q1: Name Text Input -->
                    <div role="listitem">
                        <div role="heading">Full Name</div>
                        <span aria-label="Required question">*</span>
                        <input type="text" class="whsOnd" />
                    </div>

                    <!-- Q2: Email Text Input -->
                    <div role="listitem">
                        <div role="heading">Email Address</div>
                        <input type="text" class="whsOnd" />
                    </div>

                    <!-- Q3: Phone Input -->
                    <div role="listitem">
                        <div role="heading">Phone Number</div>
                        <input type="text" class="whsOnd" />
                    </div>

                    <!-- Q4: Experience Radio Group -->
                    <div role="listitem">
                        <div role="heading">Total Years of Experience</div>
                        <div role="radiogroup">
                            <div role="radio" data-value="0-2 Years"><span class="aDTYNe">0-2 Years</span></div>
                            <div role="radio" data-value="3-5 Years"><span class="aDTYNe">3-5 Years</span></div>
                            <div role="radio" data-value="5+ Years"><span class="aDTYNe">5+ Years</span></div>
                        </div>
                    </div>

                    <!-- Q5: Notice Period Radio Group -->
                    <div role="listitem">
                        <div role="heading">Notice Period</div>
                        <div role="radiogroup">
                            <div role="radio" data-value="Immediate"><span class="aDTYNe">Immediate</span></div>
                            <div role="radio" data-value="30 Days"><span class="aDTYNe">30 Days</span></div>
                            <div role="radio" data-value="60 Days"><span class="aDTYNe">60 Days</span></div>
                        </div>
                    </div>

                    <!-- Q6: Relocation Radio -->
                    <div role="listitem">
                        <div role="heading">Are you willing to relocate to Bangalore?</div>
                        <div role="radiogroup">
                            <div role="radio" data-value="Yes"><span class="aDTYNe">Yes</span></div>
                            <div role="radio" data-value="No"><span class="aDTYNe">No</span></div>
                        </div>
                    </div>

                    <!-- Q7: Reason for change Textarea -->
                    <div role="listitem">
                        <div role="heading">Why are you looking for a change?</div>
                        <textarea class="KHxj8b"></textarea>
                    </div>

                    <!-- Submit Button -->
                    <div role="button" id="submit-btn" onclick="document.body.innerHTML='<div class=\\'vHW8K\\'>Your response has been recorded</div>'">
                        <span>Submit</span>
                    </div>
                </div>
            </form>
        </body>
        </html>
        """
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
            f.write(mock_html)
            mock_file_path = f.name

        try:
            from playwright.sync_api import sync_playwright
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            with sync_playwright() as p:
                launch_kwargs = {"headless": True}
                if os.path.exists(chrome_path):
                    launch_kwargs["executable_path"] = chrome_path
                browser = p.chromium.launch(**launch_kwargs)
                page = browser.new_page()
                page.goto(f"file://{mock_file_path}")

                solver = forms_agent.GoogleFormsSolver(
                    page, self.config, dry_run=False, review_mode=False
                )
                success = solver.solve()
                self.assertTrue(success, "Google Forms solver failed to complete form.")
                self.assertGreaterEqual(solver.filled_count, 6, "Expected at least 6 fields filled.")

                # Verify confirmation page
                conf_text = page.locator("div.vHW8K").inner_text()
                self.assertEqual(conf_text, "Your response has been recorded")
                browser.close()
        finally:
            if os.path.exists(mock_file_path):
                os.remove(mock_file_path)

    def test_06_microsoft_forms_dom_solver(self):
        """Test Microsoft Forms solver on realistic mock Microsoft Forms HTML."""
        mock_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Job Candidate Intake</title></head>
        <body>
            <span class="office-form-title">Software Engineer Intake</span>
            <div data-automation-id="form-page">
                <!-- Q1: Name -->
                <div data-automation-id="questionItem">
                    <span data-automation-id="questionTitle">Candidate Name</span>
                    <input data-automation-id="textInput" type="text" />
                </div>

                <!-- Q2: Email -->
                <div data-automation-id="questionItem">
                    <span data-automation-id="questionTitle">Email ID</span>
                    <input data-automation-id="textInput" type="text" />
                </div>

                <!-- Q3: Current CTC -->
                <div data-automation-id="questionItem">
                    <span data-automation-id="questionTitle">Current CTC (in LPA)</span>
                    <input data-automation-id="textInput" type="text" />
                </div>

                <!-- Q4: Expected CTC -->
                <div data-automation-id="questionItem">
                    <span data-automation-id="questionTitle">Expected CTC (in LPA)</span>
                    <input data-automation-id="textInput" type="text" />
                </div>

                <!-- Q5: Radio - Notice Period -->
                <div data-automation-id="questionItem">
                    <span data-automation-id="questionTitle">Notice Period</span>
                    <div data-automation-id="radio"><span>Immediate</span></div>
                    <div data-automation-id="radio"><span>30 Days</span></div>
                    <div data-automation-id="radio"><span>90 Days</span></div>
                </div>

                <!-- Q6: Textarea - Summary -->
                <div data-automation-id="questionItem">
                    <span data-automation-id="questionTitle">Brief description of your experience</span>
                    <textarea data-automation-id="textInput"></textarea>
                </div>

                <!-- Submit Button -->
                <button data-automation-id="submitButton" onclick="document.body.innerHTML='<div data-automation-id=\\'thankYouMessage\\'>Your response was submitted</div>'">
                    Submit
                </button>
            </div>
        </body>
        </html>
        """
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
            f.write(mock_html)
            mock_file_path = f.name

        try:
            from playwright.sync_api import sync_playwright
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            with sync_playwright() as p:
                launch_kwargs = {"headless": True}
                if os.path.exists(chrome_path):
                    launch_kwargs["executable_path"] = chrome_path
                browser = p.chromium.launch(**launch_kwargs)
                page = browser.new_page()
                page.goto(f"file://{mock_file_path}")

                solver = forms_agent.MicrosoftFormsSolver(
                    page, self.config, dry_run=False, review_mode=False
                )
                success = solver.solve()
                self.assertTrue(success, "Microsoft Forms solver failed to complete form.")
                self.assertGreaterEqual(solver.filled_count, 5, "Expected at least 5 fields filled.")

                conf_text = page.locator("div[data-automation-id='thankYouMessage']").inner_text()
                self.assertEqual(conf_text, "Your response was submitted")
                browser.close()
        finally:
            if os.path.exists(mock_file_path):
                os.remove(mock_file_path)


if __name__ == "__main__":
    unittest.main()
