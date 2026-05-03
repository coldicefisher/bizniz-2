"""Prompts for the UX Designer agent.

Two-phase flow:
  1. SCREENSHOT_SCRIPT_PROMPT — asks the AI to generate a Playwright script
     that navigates all views and takes screenshots.
  2. EVALUATE_PROMPT — sends screenshots to vision AI for design evaluation.
  3. FIX_PROMPT — turns evaluation findings into concrete code changes.
"""

SCREENSHOT_SCRIPT_PROMPT = """\
You are generating a Playwright screenshot script for a {framework} frontend.

The application is accessible at FRONTEND_URL (set via environment variable).
The problem statement describes what the app does:

{problem_statement}

The milestone scope for the current build:
{milestone_scope}

{routes_section}

Generate a CommonJS (.cjs) Playwright script that:
1. Visits every known route/view in the application
2. Waits for the page to fully render (networkidle or domcontentloaded)
3. Takes a full-page screenshot of each view, saved to /workspace/screenshots/<view_name>.png
4. If there are forms, fill them with sample data and screenshot the filled state
5. If there are modals or dropdowns, open them and screenshot
6. Names screenshots descriptively: home.png, services-list.png, booking-form.png, etc.

IMPORTANT:
- Use `const {{ test, expect }} = require('@playwright/test');` (CommonJS)
- Use `process.env.FRONTEND_URL` for the base URL
- Create the screenshots directory: `const fs = require('fs'); fs.mkdirSync('/workspace/screenshots', {{ recursive: true }});`
- Set viewport to 1280x720 for consistent screenshots
- Add 1-second waits after navigation for SPA hydration
- Do NOT assert on content — this is purely for screenshots, not testing
- If a page doesn't load, skip it (try/catch) and move on
- Maximum 15 screenshots to keep evaluation cost reasonable

Output ONLY the script code, no markdown fences, no explanation.
"""

EVALUATE_PROMPT = """\
You are a senior UX designer reviewing screenshots of a web application.

The application: {app_description}
Framework: {framework}
Design system: {design_system}

Review each screenshot and evaluate:

1. **Layout & Spacing**: Is content well-organized? Proper margins/padding?
   No overlapping elements? Responsive-looking structure?

2. **Typography**: Readable font sizes? Clear hierarchy (headings vs body)?
   Consistent font usage?

3. **Color & Contrast**: Sufficient contrast for readability? Consistent
   color palette? Accessible color choices?

4. **Navigation**: Clear nav structure? Active state visible? Breadcrumbs
   where needed?

5. **Forms & Inputs**: Properly labeled? Error states visible? Good sizing
   for touch targets? Placeholder text helpful?

6. **Empty States**: Does the app handle empty data gracefully? Helpful
   messages when no content?

7. **Overall Polish**: Does it look like a real application or a homework
   assignment? Professional appearance?

For each issue found, provide a SPECIFIC code fix. Reference exact files
and CSS classes/components when possible.

Rate the overall design quality on a scale of 1-10.
"""

EVALUATE_SCHEMA = {
    "name": "ux_evaluation",
    "schema": {
        "type": "object",
        "properties": {
            "overall_score": {
                "type": "integer",
                "description": "Design quality score from 1 (unusable) to 10 (polished)",
            },
            "summary": {
                "type": "string",
                "description": "One-paragraph summary of the design state",
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "major", "minor", "cosmetic"],
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "layout", "typography", "color", "navigation",
                                "forms", "empty_states", "spacing", "responsiveness",
                            ],
                        },
                        "screenshot": {
                            "type": "string",
                            "description": "Which screenshot this issue appears in",
                        },
                        "description": {
                            "type": "string",
                            "description": "What's wrong",
                        },
                        "fix_description": {
                            "type": "string",
                            "description": "Specific code change to fix this issue",
                        },
                        "target_file": {
                            "type": "string",
                            "description": "Relative file path to modify (e.g. src/App.tsx, src/index.css)",
                        },
                    },
                    "required": ["severity", "category", "description", "fix_description"],
                },
            },
        },
        "required": ["overall_score", "summary", "issues"],
    },
}

FIX_PROMPT_TEMPLATE = """\
You are fixing UX issues in a {framework} frontend application.

The UX designer reviewed screenshots and found these issues:

{issues_block}

Apply ALL fixes. Focus on:
- CSS changes for layout/spacing/typography/color issues
- Component structure changes for navigation/form issues
- Adding empty state components where needed

The design system is {design_system}. Use its utilities and components
where possible rather than raw CSS.

Current file contents will be provided via the workspace. Make targeted
edits — do not rewrite files from scratch.
"""
