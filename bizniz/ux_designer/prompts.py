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

{auth_contract_section}

GOAL: capture a screenshot of EVERY user-visible view so a designer can
evaluate the UI. Each route is its own independent test so that one stuck
or broken page does NOT prevent the others from being captured.

STRUCTURE — emit one `test('screenshot <name>', ...)` block per route:

    const {{ test }} = require('@playwright/test');
    const fs = require('fs');
    const BASE = process.env.FRONTEND_URL;

    test.beforeAll(() => {{
      fs.mkdirSync('/workspace/screenshots', {{ recursive: true }});
    }});

    async function captureRoute(page, route, filename) {{
      await page.setViewportSize({{ width: 1280, height: 720 }});
      await page.goto(`${{BASE}}${{route}}`, {{ waitUntil: 'domcontentloaded', timeout: 30000 }});
      await page.waitForLoadState('networkidle', {{ timeout: 30000 }}).catch(() => {{}});
      await page.waitForFunction(() => {{
        const root = document.querySelector('#root, #app, main') || document.body;
        if (!root) return false;
        const text = (root.innerText || '').trim();
        const interactive = root.querySelectorAll(
          'button, a, input, textarea, select, h1, h2, h3, [role]'
        ).length;
        return text.length > 20 || interactive > 0;
      }}, {{ timeout: 30000, polling: 1000 }});
      await page.screenshot({{ path: `/workspace/screenshots/${{filename}}.png`, fullPage: true }});
    }}

    test('screenshot home', async ({{ page }}) => {{ await captureRoute(page, '/', 'home'); }});
    test('screenshot login', async ({{ page }}) => {{ await captureRoute(page, '/login', 'login'); }});
    // ...one test per route...

AUTHENTICATION (when an AUTH CONTRACT is provided above):
Many routes are protected and redirect to `/login` when unauthenticated.
Sign in ONCE in `test.beforeAll`, save `storageState`, then
`test.use({{ storageState }})`. CRITICAL RULES:

1. ALWAYS write the storageState file at the end of beforeAll, even if
   login failed. Use a `finally` block. Otherwise tests cascade with
   ENOENT and zero screenshots are captured.
2. Use POSITION-BASED locators (NOT guessed name="email" / name="password"
   attributes — frontends often don't set those). Fall through a small
   list of common shapes.
3. {login_source_section}

Pattern (use this verbatim, only changing the credentials and route names):

    const STATE_PATH = '/workspace/.ux-auth-state.json';

    test.beforeAll(async ({{ browser }}) => {{
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      try {{
        await page.goto(`${{BASE}}/login`, {{ waitUntil: 'domcontentloaded', timeout: 30000 }});

        // Position-based: first text/email input, first password input, first submit.
        // This is MUCH more robust than guessing name attributes.
        const userInput = page.locator(
          'input[type="email"], input[type="text"], input[name="email"], input[name="username"]'
        ).first();
        const passInput = page.locator('input[type="password"]').first();
        await userInput.fill('<TEST_EMAIL_OR_USERNAME>');
        await passInput.fill('<TEST_PASSWORD>');

        const submit = page.locator('button[type="submit"], button:has-text("Login"), button:has-text("Sign in")').first();
        await Promise.all([
          page.waitForURL((url) => !url.pathname.endsWith('/login'), {{ timeout: 15000 }}).catch(() => {{}}),
          submit.click(),
        ]);
      }} catch (e) {{
        console.error('UI login failed:', e.message);

        // Fallback: API login + inject token into localStorage.
        const apiBase = process.env.BACKEND_URL;
        if (apiBase) {{
          try {{
            const resp = await page.request.post(`${{apiBase}}/api/v1/auth/login`, {{
              data: {{ email: '<TEST_EMAIL_OR_USERNAME>', username: '<TEST_EMAIL_OR_USERNAME>', password: '<TEST_PASSWORD>' }},
            }});
            if (resp.ok()) {{
              const body = await resp.json();
              const token = body.access_token || body.accessToken || body.token || body.jwt;
              await page.goto(BASE);
              await page.evaluate((t) => {{
                if (!t) return;
                localStorage.setItem('access_token', t);
                localStorage.setItem('accessToken', t);
                localStorage.setItem('token', t);
              }}, token);
            }}
          }} catch (e2) {{
            console.error('API login also failed:', e2.message);
          }}
        }}
      }} finally {{
        // ALWAYS save state — even an unauthenticated state file prevents
        // every subsequent test from failing with ENOENT.
        await ctx.storageState({{ path: STATE_PATH }});
        await ctx.close();
      }}
    }});

    // Apply auth state to every test in this file.
    test.use({{ storageState: STATE_PATH }});

If NO auth contract is provided, omit the auth setup entirely and just
screenshot whatever public routes are visible.

REQUIREMENTS:
- One `test()` block per route — NEVER combine multiple routes into one test.
  Playwright runs each test in a fresh page/context, so a hang on one
  route does not poison the others.
- Use `waitUntil: 'domcontentloaded'` (NOT 'networkidle') on `page.goto`
  — long-polling endpoints can prevent networkidle from firing.
- ALWAYS wait for real content with `waitForFunction` before screenshotting.
  Do NOT use a fixed `waitForTimeout` — it either over-waits or captures blank.
- For forms, add a SEPARATE test that visits the route, fills sample data,
  then screenshots (`form-name-filled.png`).
- For modals/dropdowns, add a SEPARATE test that opens them.
- Name screenshots descriptively: `home.png`, `properties-list.png`,
  `payments-history.png`, `maintenance-form-filled.png`.
- Cap total tests at 15 to keep evaluation cost reasonable.
- Use `process.env.FRONTEND_URL` for the base URL.
- Use CommonJS: `const {{ test }} = require('@playwright/test');`.

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
