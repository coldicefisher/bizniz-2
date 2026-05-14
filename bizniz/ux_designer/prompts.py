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
      // ALSO write a metadata JSON next to the PNG so the Python
      // harness can deterministically verify we captured the right
      // page (final URL after redirects, what the headings say). On
      // mismatch the harness skips the expensive Coder fix step for
      // this iteration.
      try {{
        const finalUrl = page.url();
        const title = await page.title().catch(() => '');
        const headings = await page.locator('h1, h2, h3').allInnerTexts().catch(() => []);
        const bodyText = await page.locator('body').innerText().catch(() => '');
        const meta = {{
          requested_route: route,
          final_url: finalUrl,
          final_pathname: new URL(finalUrl).pathname,
          title,
          headings: headings.slice(0, 8),
          body_sample: (bodyText || '').slice(0, 600),
        }};
        require('fs').writeFileSync(
          `/workspace/screenshots/${{filename}}.meta.json`,
          JSON.stringify(meta, null, 2),
        );
      }} catch (e) {{ /* meta write is best-effort */ }}
    }}

    test('screenshot home', async ({{ page }}) => {{ await captureRoute(page, '/', 'home'); }});
    test('screenshot login', async ({{ page }}) => {{ await captureRoute(page, '/login', 'login'); }});
    // ...one test per route...

AUTHENTICATION (when an AUTH CONTRACT is provided above):
Many routes are protected and redirect to `/login` when unauthenticated.

**MANDATORY** when an AUTH CONTRACT exists above (not optional):
  - Generate the ``test.beforeAll`` storageState block shown below.
  - Generate ``test.use({{ storageState: STATE_PATH }})`` at module
    scope so EVERY single ``test()`` in the file inherits the
    authenticated session.
  - DO NOT generate per-test login helpers. The storageState
    approach is the only correct shape — per-test logins waste
    minutes and frequently silently fail, leaving the test to
    capture the login page instead of the real route.
  - Public routes (``/``, ``/login``, ``/register``) ALSO get the
    shared storageState — that's fine, public routes ignore it.

Use the seeded admin credentials from the AUTH CONTRACT's "Test
users" section (typically email+password, role=admin). The admin
role is the right choice because admin sees every route's content
(non-admin gets 403 on /admin/* and would still capture errors).

CRITICAL RULES:

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

ROUTE PARAMETERS (``:id``, ``:slug``, ``:userId``, etc.):
A route like ``/recipes/:id`` is a TEMPLATE, not a real URL. Visiting
it literally (e.g. ``page.goto('/recipes/:id')``) yields a 404 or a
broken route-not-found page. Resolve the placeholder to a real value
BEFORE navigating.

**REQUIRED for any route containing a ``:placeholder``: seed via API
in beforeAll** and stash the returned id at module scope. Use the
seeded id for every dynamic-route test. This is the only approach
that's reliable across empty-state vs populated stacks. Pattern:

    let SEEDED_RECIPE_ID = null;

    test.beforeAll(async ({{ browser }}) => {{
      // ... (existing storageState auth block) ...
      // Seed AFTER login state is saved so we get an authed POST.
      try {{
        const apiBase = process.env.BACKEND_URL || BASE;
        // Pull the auth token from storageState — the request needs
        // the bearer header that the SPA's apiClient would send.
        const stateJson = JSON.parse(
          require('fs').readFileSync(STATE_PATH, 'utf-8')
        );
        const origins = stateJson.origins || [];
        let token = null;
        for (const origin of origins) {{
          for (const item of origin.localStorage || []) {{
            if (/(token|jwt)/i.test(item.name)) {{ token = item.value; break; }}
          }}
        }}
        const resp = await page.request.post(`${{apiBase}}/api/recipes`, {{
          headers: token ? {{ Authorization: `Bearer ${{token}}` }} : {{}},
          data: {{ /* minimal valid payload for this domain */ }},
        }});
        if (resp.ok()) SEEDED_RECIPE_ID = (await resp.json()).id;
      }} catch (e) {{
        console.error('Seed failed:', e.message);
      }}
    }});

    test('screenshot recipe detail', async ({{ page }}) => {{
      if (!SEEDED_RECIPE_ID) {{
        console.error('No seeded id; skipping detail capture');
        return;
      }}
      await captureRoute(page, `/recipes/${{SEEDED_RECIPE_ID}}`, 'recipe-detail');
    }});

    test('screenshot recipe edit', async ({{ page }}) => {{
      if (!SEEDED_RECIPE_ID) return;
      await captureRoute(page, `/recipes/${{SEEDED_RECIPE_ID}}/edit`, 'recipe-edit');
    }});

DO NOT rely on Strategy A (clicking a list link) for dynamic routes
— on empty stacks the only matching link is often a CTA like
``/recipes/new``, which captures the wrong page. Strategy A is
acceptable only as a FALLBACK after the API seed has failed.

Strategy A — fallback only:

      test('screenshot recipe detail', async ({{ page }}) => {{
        await page.setViewportSize({{ width: 1280, height: 720 }});
        await page.goto(`${{BASE}}/dashboard`, {{ waitUntil: 'domcontentloaded', timeout: 30000 }});
        await waitForRendered(page);
        // Click the first link whose href matches /recipes/<something>.
        // Adjust the selector for your app's actual link shape.
        const detailLink = page.locator('a[href^="/recipes/"]').first();
        if (await detailLink.count() === 0) {{
          console.error('No recipe detail links found; skipping detail screenshot');
          return;
        }}
        await Promise.all([
          page.waitForURL(/\\/recipes\\/[^/]+$/, {{ timeout: 15000 }}),
          detailLink.click(),
        ]);
        await waitForRendered(page);
        await page.screenshot({{ path: `/workspace/screenshots/recipe-detail.png`, fullPage: true }});
      }});

  **Strategy B — API seed + substitute (fallback)**:
  When no list view exists yet, or the list is empty, POST to the
  backend to create a record and use the returned id. Read the API
  shape from the backend code or the OpenAPI doc if you have it.

      test('screenshot recipe detail (seeded)', async ({{ page }}) => {{
        const apiBase = process.env.BACKEND_URL || `${{BASE}}`;
        const seedResp = await page.request.post(`${{apiBase}}/api/recipes`, {{
          headers: {{ /* auth header from storageState or token */ }},
          data: {{ /* minimal valid recipe payload */ }},
        }});
        if (!seedResp.ok()) {{
          console.error('Seed failed:', seedResp.status());
          return;
        }}
        const recipeId = (await seedResp.json()).id;
        await captureRoute(page, `/recipes/${{recipeId}}`, 'recipe-detail');
      }});

CRITICAL: NEVER write ``page.goto('.../:id')`` or any path that has a
literal ``:placeholder`` segment. The Playwright sidecar will navigate
to a 404 and we'll capture the error page, which scores 1-2/10 and
churns the design iteration on the wrong problem. Detect every
``:param`` in the route list above and resolve it before navigation.

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
- Name screenshots descriptively using nouns from the problem statement:
  `home.png`, `<entity>-list.png`, `<entity>-form-filled.png`, etc.
  Do NOT use names from unrelated domains.
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
