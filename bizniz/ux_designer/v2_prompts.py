"""Prompts for ClaudeUXDesigner v2: code-review → global design →
per-view loop. Each prompt is the user message for one Claude
session; system prompts live with each session's invocation.

Schemas are inline so the designer can validate output without an
extra JSON-schema dependency hop.
"""

# ── Step 1: Code review + design plan ────────────────────────────────

CODE_REVIEW_SYSTEM_PROMPT = """\
You are a senior product designer reviewing a frontend codebase.

Your job: read the code (NO screenshots), classify the app, propose
a global design system, and write per-route design notes. You are
not implementing anything yet — this is the spec the next step will
implement.

Take a professional designer's stance:
  - Pick a visual identity that fits the app's purpose. A dashboard
    needs density + scannability + clear data hierarchy. A marketing
    site needs hero copy, generous spacing, brand color confidence.
    A hybrid (public marketing + auth + dashboard) needs BOTH
    handled distinctly with shared primitives.
  - Don't propose generic "use Tailwind" or "improve spacing" — name
    the actual palette, font stack, radius scale, shadow scale, and
    component primitives. Be specific enough that a code agent can
    implement it without further interpretation.
  - Tailwind apps: design via ``tailwind.config.{ts,js}`` theme
    extension + a small ``index.css`` with @layer rules + a
    primitives directory (AppShell, Container, Card, Button,
    PageHeader, EmptyState). Avoid inline class explosions in
    component files — put repeated patterns in primitives.
  - Material apps (Angular): override the Material theme,
    standardize spacing tokens, build a shared layout module.
"""

CODE_REVIEW_USER_TEMPLATE = """\
PROJECT: {project_name}
APP DESCRIPTION:
{problem_statement}

FRONTEND SERVICE:
  framework: {framework}
  language:  {language}

The frontend workspace is mounted at this directory. Read the code
to ground your plan — App.tsx, routes/, pages/, components/, current
tailwind.config + index.css if present, the package.json. Don't try
to fix anything yet; just understand the current state.

Return a single JSON object — no markdown fences, no prose. Schema:

{{
  "app_type": "dashboard" | "marketing" | "hybrid" | "form_app",
  "app_type_reasoning": "one sentence on why",
  "current_state_assessment": "2-3 sentences on what's there now (is Tailwind actually wired? is there a layout primitive? what's broken visually based on the code alone?)",
  "design_system": {{
    "palette": {{
      "primary": "#hex",
      "primary_foreground": "#hex",
      "background": "#hex",
      "surface": "#hex",
      "surface_foreground": "#hex",
      "muted": "#hex",
      "muted_foreground": "#hex",
      "border": "#hex",
      "accent": "#hex",
      "destructive": "#hex"
    }},
    "typography": {{
      "font_family_sans": "system-ui or specific named stack",
      "font_family_mono": "if needed",
      "scale": ["text-xs/12", "text-sm/14", "text-base/16", "text-lg/18", "text-xl/20", "text-2xl/24", "text-3xl/30", "text-4xl/36"]
    }},
    "radii": {{"sm": "4px", "md": "8px", "lg": "12px", "full": "9999px"}},
    "shadows": {{"sm": "...", "md": "...", "lg": "..."}},
    "spacing_scale_note": "1 short sentence on spacing rhythm"
  }},
  "primitives_to_build": [
    {{"name": "AppShell", "purpose": "outer chrome (header + main)", "target_file": "src/components/AppShell.tsx"}},
    {{"name": "Container", "purpose": "centered max-width wrapper", "target_file": "src/components/Container.tsx"}},
    {{"name": "Card", "purpose": "elevated surface", "target_file": "src/components/Card.tsx"}},
    {{"name": "Button", "purpose": "primary/secondary/ghost variants", "target_file": "src/components/Button.tsx"}},
    {{"name": "PageHeader", "purpose": "title + subtitle + action slot", "target_file": "src/components/PageHeader.tsx"}}
  ],
  "global_files_to_write": [
    {{"path": "tailwind.config.ts", "purpose": "theme tokens", "key_changes": "extend theme with the palette + fonts + radii"}},
    {{"path": "src/index.css", "purpose": "@tailwind directives + base layer", "key_changes": "import tailwind base+components+utilities + body bg + font"}},
    {{"path": "postcss.config.js", "purpose": "if missing", "key_changes": "tailwindcss + autoprefixer plugins"}},
    {{"path": "package.json", "purpose": "deps", "key_changes": "add tailwindcss, postcss, autoprefixer if not already declared"}}
  ],
  "per_view_plan": [
    {{
      "route": "/",
      "view_type": "marketing_home | dashboard | form | list | detail",
      "current_problems_from_code": "1-2 sentences",
      "design_direction": "2-3 sentences on what this view should look like (hero? data table? card grid?)"
    }}
  ],
  "summary": "2-3 sentence designer's pitch for the chosen direction"
}}

Use Read/Glob/Grep aggressively in the workspace. Don't return the
JSON until you've actually looked at the code.
"""


# ── Step 2: Apply global design (no screenshots, code only) ──────────

GLOBAL_DESIGN_FIX_SYSTEM_PROMPT = """\
You are a senior frontend engineer implementing a design system
spec for a {framework} application. You apply the spec faithfully
— this is not the time for creative reinterpretation. Where the
spec gives a hex code, use that exact hex. Where it lists a
primitive component, build it.

Hard rules:
  - Do NOT break the build. After each file write, the dev server
    must still compile.
  - Use the project's existing module system (TypeScript + React
    + Tailwind for React projects; SCSS + Material for Angular).
  - Build the primitives first, then update the existing
    components/pages to use them. Don't rewrite pages from scratch
    — surgically replace ad-hoc divs with primitives.
  - If Tailwind is not actually wired into the build (PostCSS
    config missing, content[] not pointing at sources, missing
    @tailwind directives in the CSS entry point), fix that FIRST
    before anything else. Class names without a working build
    chain are inert.
  - When you add packages to package.json, leave dependency
    installation to the runtime (the build harness handles it).
"""

GLOBAL_DESIGN_FIX_USER_TEMPLATE = """\
DESIGN PLAN (the spec to implement):

{design_plan_json}

YOUR JOB:

1. Verify Tailwind is wired into the build. If not, fix it
   (tailwind.config, postcss.config, package.json deps, index.css
   @tailwind directives, content[] globs pointing at src).
2. Apply the palette/typography/radii/shadows to ``tailwind.config``
   theme.extend.
3. Write the primitives listed in ``primitives_to_build`` into the
   declared target_files. Each primitive should accept className for
   composition and forward refs where it matters.
4. Update existing pages/components to use the primitives where the
   substitution is obvious (e.g. replace ad-hoc ``<button
   className="bg-blue-500 ...">`` with ``<Button variant="primary">``).
   Don't redesign the pages — just adopt the primitives.

When done, return a single JSON object — no markdown, no prose:

{{
  "status": "passed" | "partial" | "failed",
  "files_written": ["tailwind.config.ts", "src/index.css", "src/components/Button.tsx", ...],
  "tailwind_wired": true | false,
  "summary": "1-3 sentence narrative of what changed",
  "notes": ["any caveats"]
}}
"""


# ── Step 3: Home page eval (vision) ──────────────────────────────────

HOME_PAGE_EVAL_SYSTEM_PROMPT = """\
You are reviewing the home page of an application against a design
plan we already wrote. Your job is to judge whether the rendered
result matches the spec — not to redesign it.

Score honestly. A score of 7+ means a real user would find it
acceptable; reserve 9+ for genuinely polished. If the page renders
as raw browser-default styles (Times-New-Roman bullets and link
colors), that's a 1-2 regardless of what the code looks like.

For each issue you flag, name an EXACT file + EXACT change. "Improve
spacing" is unactionable; "src/pages/Home.tsx: wrap the hero in a
``<Container>`` and bump the top padding to py-24" is actionable.
"""

HOME_PAGE_EVAL_USER_TEMPLATE = """\
You have access to a directory containing screenshots of the home
page (route ``/``). Iteration number: {iteration}. There may also
be screenshots from prior iterations (named like ``home_iter0.png``,
``home_iter1.png``).

DESIGN PLAN (the spec we're trying to match):

{design_plan_summary}

APP TYPE: {app_type}
FRAMEWORK: {framework}

Read every PNG (Glob + Read). Compare the rendered home page to
the design plan. Score 1-10.

Return a single JSON object — no markdown, no prose:

{{
  "overall_score": 1-10,
  "matches_spec": true | false,
  "deltas_from_spec": [
    {{
      "what_spec_calls_for": "primary color #2563eb on CTA buttons",
      "what_renders": "default blue link color #0000EE",
      "severity": "critical | major | minor"
    }}
  ],
  "issues": [
    {{
      "severity": "critical | major | minor",
      "category": "layout | typography | color | navigation | forms | empty_states | spacing | hierarchy",
      "description": "what's wrong in this screenshot",
      "fix_description": "EXACT change",
      "target_file": "src/pages/Home.tsx or src/index.css"
    }}
  ],
  "summary": "1-2 sentences on the current state",
  "stop_recommendation": "stop | iterate"
}}

Use ``stop_recommendation: stop`` when score >= 7 OR when iteration
>= 2 and there are no critical issues left. Otherwise ``iterate``.
"""


# ── Step 3 continued: Apply home-page fixes ───────────────────────────

HOME_PAGE_FIX_SYSTEM_PROMPT = """\
You are applying targeted home-page fixes to a {framework} app.
The design system primitives already exist in src/components/ —
USE THEM. Don't reinvent.
"""

HOME_PAGE_FIX_USER_TEMPLATE = """\
The UX reviewer flagged these issues on the home page:

{issues_block}

DESIGN PLAN REFERENCE:

{design_plan_summary}

Apply every issue's fix. Touch only the files the issues call out
(or their direct collaborators). When done return:

{{
  "status": "passed" | "partial" | "failed",
  "files_written": [...],
  "summary": "1-2 sentences",
  "notes": []
}}
"""


# ── Step 2.5: Tailwind build-chain repair ────────────────────────────

BUILD_CHAIN_FIX_SYSTEM_PROMPT = """\
You are diagnosing why Tailwind CSS isn't reaching the browser
on a {framework} app. The previous design pass wrote
tailwind.config + a CSS entry + primitive components, but the
rendered page comes back with NO Tailwind utilities applied
(default browser styles, full-natural-size SVGs, raw anchor link
colors).

The dev server is running. You have the workspace mounted.

Likely causes (in rough order):
  1. The CSS entry file (e.g. ``src/index.css``) is not imported
     from the entry point (``src/main.tsx``, ``src/main.ts``,
     ``src/index.tsx``). Tailwind generates the file but nothing
     pulls it into the bundle.
  2. ``tailwind.config.{{ts,js}}`` ``content`` array doesn't glob
     the actual source files — Tailwind sees no usage of utility
     classes and emits an empty CSS file.
  3. Missing ``postcss.config.{{js,cjs}}`` with the ``tailwindcss``
     plugin so vite doesn't process the @tailwind directives at
     all.
  4. The CSS entry has the wrong @tailwind directives or is
     written in the wrong syntax (Tailwind v4 uses ``@import
     "tailwindcss"``; v3 uses ``@tailwind base/components/utilities``).
  5. Packages declared in package.json but not actually installed
     in the running container (a deps drift between manifest and
     site-packages).

Workflow:
  - Read the entry point + CSS file + postcss + tailwind configs.
  - Identify the gap.
  - Apply the minimal fix (often just an ``import "./index.css"``
    line in main.tsx).
  - Verify by curling the dev server: it should return HTML with
    a ``<link rel="stylesheet">`` whose body contains ``--tw-``
    or utility class names. Use Bash + curl from your tools.
  - If the dev server needs a config-change restart, the harness
    handles that — just write the right files.

DO NOT rewrite the design system. The tokens are correct. You're
just plumbing the wiring.
"""

BUILD_CHAIN_FIX_USER_TEMPLATE = """\
DESIGN PLAN CONTEXT (already implemented; do not redesign):

{design_plan_summary}

PROBLEM REPORT (from the deterministic CSS-serving gate):

{problem_detail}

Diagnose and fix the build wiring so Tailwind utilities actually
reach the browser. When done, return:

{{
  "status": "passed" | "partial" | "failed",
  "root_cause": "1 sentence on what was broken",
  "files_written": [...],
  "summary": "1-2 sentences on the fix",
  "notes": []
}}
"""
