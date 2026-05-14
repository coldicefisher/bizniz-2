"""UX Designer agent.

Screenshots frontend views via Playwright sidecar, evaluates design quality
via Gemini vision, and dispatches code fixes through the Coder agent.

Pipeline placement: after image rebuild, before integration tests.
The stack must be running for screenshots.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.gemini.gemini_client import GeminiClient
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.ux_designer.prompts import (
    SCREENSHOT_SCRIPT_PROMPT,
    EVALUATE_PROMPT,
    EVALUATE_SCHEMA,
    FIX_PROMPT_TEMPLATE,
)

if TYPE_CHECKING:
    from bizniz.workspace.base_workspace import BaseWorkspace

PLAYWRIGHT_SIDECAR_IMAGE = "bizniz-test-playwright:latest"

# Minimum score to skip fixes — if the design is already good, don't touch it.
ACCEPTABLE_SCORE = 6


def _log(on_status: Optional[Callable[[str], None]], msg: str) -> None:
    if on_status:
        on_status(msg)


class UXDesigner:
    """Screenshot → Evaluate → Fix loop for frontend services.

    Parameters
    ----------
    vision_client:
        A GeminiClient instance (must support get_text_with_images).
    coder_factory:
        Callable(workspace) → Coder agent for applying fixes.
    on_status:
        Log callback.
    max_fix_iterations:
        How many evaluate → fix → re-screenshot cycles to run.
    """

    def __init__(
        self,
        vision_client: GeminiClient,
        coder_factory: Optional[Callable] = None,
        on_status: Optional[Callable[[str], None]] = None,
        max_fix_iterations: int = 2,
        acceptable_score: int = ACCEPTABLE_SCORE,
    ):
        self._vision = vision_client
        self._vision._caller_agent = "ux_designer"
        self._coder_factory = coder_factory
        self._on_status = on_status
        self._max_fix_iterations = max_fix_iterations
        self._acceptable_score = acceptable_score

    def review_frontend(
        self,
        service: ServiceDefinition,
        workspace: "BaseWorkspace",
        compose_path: str,
        problem_statement: str,
        milestone_scope: str = "",
        design_system: str = "Tailwind CSS",
        routes: Optional[List[str]] = None,
        auth_contract: Optional[str] = None,
        backend_url: Optional[str] = None,
    ) -> Dict:
        """Run the full UX review cycle for one frontend service.

        Returns a dict with evaluation results and whether fixes were applied.
        """
        _log(self._on_status, f"UX Designer: reviewing '{service.name}'...")

        result = {
            "service": service.name,
            "iterations": 0,
            "initial_score": None,
            "final_score": None,
            "fixes_applied": 0,
            "screenshots_taken": 0,
        }

        for iteration in range(1, self._max_fix_iterations + 1):
            result["iterations"] = iteration

            # Step 1: Take screenshots
            screenshots = self._take_screenshots(
                service=service,
                workspace=workspace,
                compose_path=compose_path,
                problem_statement=problem_statement,
                milestone_scope=milestone_scope,
                routes=routes,
                auth_contract=auth_contract,
                backend_url=backend_url,
            )
            result["screenshots_taken"] = len(screenshots)

            if not screenshots:
                _log(self._on_status, f"UX Designer: no screenshots captured for '{service.name}', skipping")
                break

            _log(self._on_status, f"UX Designer: captured {len(screenshots)} screenshot(s), evaluating...")

            # Step 2: Evaluate via vision
            evaluation = self._evaluate_screenshots(
                screenshots=screenshots,
                service=service,
                problem_statement=problem_statement,
                design_system=design_system,
            )

            score = evaluation.get("overall_score", 0)
            issues = evaluation.get("issues", [])
            _log(
                self._on_status,
                f"UX Designer: score={score}/10, {len(issues)} issue(s) — "
                f"{evaluation.get('summary', '')[:120]}"
            )

            if result["initial_score"] is None:
                result["initial_score"] = score
            result["final_score"] = score

            # Step 3: If acceptable or no fixable issues, stop
            fixable = [i for i in issues if i.get("severity") in ("critical", "major")]
            if score >= self._acceptable_score and not fixable:
                _log(self._on_status, f"UX Designer: score {score} is acceptable, no critical/major issues")
                break

            if not fixable and not issues:
                _log(self._on_status, f"UX Designer: no issues found despite low score, skipping fixes")
                break

            # Step 4: Apply fixes via Coder
            if self._coder_factory is None:
                _log(self._on_status, f"UX Designer: no coder_factory — cannot apply fixes")
                result["evaluation"] = evaluation
                break

            fixes_applied = self._apply_fixes(
                issues=issues,
                service=service,
                workspace=workspace,
                design_system=design_system,
            )
            result["fixes_applied"] += fixes_applied

            if fixes_applied == 0:
                _log(self._on_status, "UX Designer: coder applied no fixes, stopping")
                break

            _log(
                self._on_status,
                f"UX Designer: applied {fixes_applied} fix(es), "
                f"{'re-evaluating...' if iteration < self._max_fix_iterations else 'done'}"
            )

        result["evaluation"] = evaluation if 'evaluation' in dir() else {}
        return result

    def _take_screenshots(
        self,
        service: ServiceDefinition,
        workspace: "BaseWorkspace",
        compose_path: str,
        problem_statement: str,
        milestone_scope: str = "",
        routes: Optional[List[str]] = None,
        auth_contract: Optional[str] = None,
        backend_url: Optional[str] = None,
    ) -> List[Dict]:
        """Generate a Playwright script and run it to capture screenshots.

        Returns list of {"name": str, "path": Path, "bytes": bytes}.
        """
        # Generate the screenshot script via AI
        routes_section = ""
        if routes:
            routes_section = "Known routes:\n" + "\n".join(f"  - {r}" for r in routes)
        else:
            # Try to discover routes from workspace files
            routes_section = self._discover_routes(workspace, service)

        if auth_contract:
            auth_contract_section = (
                "AUTH CONTRACT — sign in before screenshotting protected routes:\n\n"
                f"{auth_contract}\n"
            )
            login_source_section = self._login_source_hint(workspace)
        else:
            auth_contract_section = (
                "No auth contract — assume routes are public; do NOT generate "
                "any auth setup code."
            )
            login_source_section = "(no auth contract — skip auth setup entirely)"

        script_prompt = SCREENSHOT_SCRIPT_PROMPT.format(
            framework=service.framework,
            problem_statement=problem_statement,
            milestone_scope=milestone_scope or "(full application)",
            routes_section=routes_section,
            auth_contract_section=auth_contract_section,
            login_source_section=login_source_section,
        )

        script_text, _, _ = self._vision.get_text(
            messages=[
                {"role": "system", "content": "You generate Playwright screenshot scripts. Output ONLY code."},
                {"role": "user", "content": script_prompt},
            ],
            use_message_history=False,
        )

        # Strip markdown fences if present
        script_text = _strip_code_fences(script_text)

        # When an auth contract is provided, the generated script
        # MUST use the storageState pattern (test.use({storageState:
        # ...}) at module scope). Without it, protected routes
        # silently redirect to /login and we capture login pages for
        # every protected view — the symptom Stage 2 of recipe_box's
        # multi-route run surfaced.
        if auth_contract and "test.use" not in script_text and "storageState" not in script_text:
            _log(
                self._on_status,
                "UX Designer: generated script missing storageState — "
                "protected routes WILL capture as login. Re-asking with "
                "explicit reminder..."
            )
            retry_prompt = (
                script_prompt
                + "\n\nNOTE: your previous response did NOT include the "
                "``test.use({ storageState: STATE_PATH })`` line at module "
                "scope. Regenerate, this time wiring the storageState "
                "pattern so EVERY test inherits the authenticated session. "
                "Do not output per-test loginAsAdmin helpers."
            )
            script_text2, _, _ = self._vision.get_text(
                messages=[
                    {"role": "system", "content": "You generate Playwright screenshot scripts. Output ONLY code."},
                    {"role": "user", "content": retry_prompt},
                ],
                use_message_history=False,
            )
            script_text2 = _strip_code_fences(script_text2)
            if "test.use" in script_text2 or "storageState" in script_text2:
                script_text = script_text2

        # Write script to workspace
        workspace_root = Path(workspace.root)
        script_path = workspace_root / "tests" / "ux_screenshots.spec.cjs"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script_text)

        screenshots_dir = workspace_root / "screenshots"
        # Clear any leftover screenshots from prior runs so collected
        # results reflect only what this run captured.
        if screenshots_dir.exists():
            for old in screenshots_dir.glob("*.png"):
                try:
                    old.unlink()
                except Exception:
                    pass
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Run via Playwright sidecar
        success, output = self._run_screenshot_script(
            service=service,
            workspace_path=workspace_root,
            compose_path=compose_path,
            backend_url=backend_url,
        )

        if not success:
            _log(self._on_status, f"UX Designer: screenshot script failed:\n{output[-500:]}")
            # Try a simpler fallback — just screenshot the home page
            return self._fallback_screenshots(service, workspace_root, compose_path)

        # Collect captured screenshots
        return self._collect_screenshots(screenshots_dir)

    def _run_screenshot_script(
        self,
        service: ServiceDefinition,
        workspace_path: Path,
        compose_path: str,
        backend_url: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Run the screenshot Playwright script in the sidecar."""
        from bizniz.integration.runner import _compose_project_name
        project_name = _compose_project_name(compose_path)
        network = f"{project_name}_app-network"
        base_url = f"http://{service.name}:{service.port}"

        config_body = (
            'module.exports = { testDir: "tests", '
            'testMatch: ["**/ux_screenshots.spec.cjs"], '
            'reporter: "list", timeout: 45000, '
            'fullyParallel: true, workers: 4, '
            'forbidOnly: true, '
            'use: { trace: "off", video: "off", screenshot: "off", '
            'viewport: { width: 1280, height: 720 } } };'
        )
        write_config = f"printf '%s' {shlex.quote(config_body)} > playwright.ux.config.cjs"
        # Pre-create an empty/anonymous storageState so `test.use({storageState})`
        # can always load it. beforeAll overwrites with the authenticated state.
        # Without this, a slow/failed login means every test ENOENTs.
        empty_state = '{"cookies":[],"origins":[]}'
        write_state = (
            f"printf '%s' {shlex.quote(empty_state)} > .ux-auth-state.json"
        )
        env_exports = f"FRONTEND_URL={shlex.quote(base_url)} "
        if backend_url:
            env_exports += f"BACKEND_URL={shlex.quote(backend_url)} "
        run_cmd = (
            f"cd /workspace && {write_config} && {write_state} && "
            f"{env_exports}"
            f"npx playwright test --config=playwright.ux.config.cjs"
        )

        cmd = [
            "docker", "run", "--rm",
            "--network", network,
            "-v", f"{workspace_path}:/workspace",
            "-w", "/workspace",
            "--ipc=host",
            "-e", "NODE_PATH=/usr/lib/node_modules",
            PLAYWRIGHT_SIDECAR_IMAGE,
            "sh", "-c", run_cmd,
        ]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired as e:
            return False, f"screenshot script timed out after 300s\n{e.stdout or ''}{e.stderr or ''}"

        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")

    def _fallback_screenshots(
        self,
        service: ServiceDefinition,
        workspace_path: Path,
        compose_path: str,
    ) -> List[Dict]:
        """Take a simple home-page screenshot when the AI-generated script fails."""
        from bizniz.integration.runner import _compose_project_name
        project_name = _compose_project_name(compose_path)
        network = f"{project_name}_app-network"
        base_url = f"http://{service.name}:{service.port}"

        screenshots_dir = workspace_path / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        # Inline script: navigate, poll for real content, screenshot.
        # waitForFunction polls every 1s for up to 60s, so we never screenshot a
        # blank page and never over-wait once the SPA has hydrated.
        script = (
            f"const {{ chromium }} = require('playwright');\n"
            f"(async () => {{\n"
            f"  const browser = await chromium.launch();\n"
            f"  const page = await browser.newPage();\n"
            f"  await page.setViewportSize({{ width: 1280, height: 720 }});\n"
            f"  try {{\n"
            f"    await page.goto('{base_url}/', {{ waitUntil: 'domcontentloaded', timeout: 30000 }});\n"
            f"    await page.waitForLoadState('networkidle', {{ timeout: 30000 }}).catch(() => {{}});\n"
            f"    await page.waitForFunction(() => {{\n"
            f"      const root = document.querySelector('#root, #app, main') || document.body;\n"
            f"      if (!root) return false;\n"
            f"      const text = (root.innerText || '').trim();\n"
            f"      const interactive = root.querySelectorAll(\n"
            f"        'button, a, input, textarea, select, h1, h2, h3, [role]'\n"
            f"      ).length;\n"
            f"      return text.length > 20 || interactive > 0;\n"
            f"    }}, {{ timeout: 30000, polling: 1000 }});\n"
            f"    await page.screenshot({{ path: '/workspace/screenshots/home.png', fullPage: true }});\n"
            f"  }} catch(e) {{ console.error('Failed:', e.message); }}\n"
            f"  await browser.close();\n"
            f"}})();\n"
        )

        cmd = [
            "docker", "run", "--rm",
            "--network", network,
            "-v", f"{workspace_path}:/workspace",
            "-w", "/workspace",
            "--ipc=host",
            "-e", "NODE_PATH=/usr/lib/node_modules",
            PLAYWRIGHT_SIDECAR_IMAGE,
            "node", "-e", script,
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except Exception as e:
            _log(self._on_status, f"UX Designer: fallback screenshot also failed: {e}")
            return []

        return self._collect_screenshots(screenshots_dir)

    @staticmethod
    def _collect_screenshots(screenshots_dir: Path) -> List[Dict]:
        """Read all PNGs from the screenshots directory."""
        results = []
        if not screenshots_dir.exists():
            return results
        for png in sorted(screenshots_dir.glob("*.png")):
            try:
                data = png.read_bytes()
                if len(data) > 50:  # skip empty/corrupt files
                    results.append({
                        "name": png.stem,
                        "path": png,
                        "bytes": data,
                        "mime_type": "image/png",
                    })
            except Exception:
                continue
        return results

    def _evaluate_screenshots(
        self,
        screenshots: List[Dict],
        service: ServiceDefinition,
        problem_statement: str,
        design_system: str,
    ) -> Dict:
        """Send screenshots to Gemini vision for UX evaluation."""
        prompt = EVALUATE_PROMPT.format(
            app_description=problem_statement[:500],
            framework=service.framework,
            design_system=design_system,
        )

        # Add screenshot names to the prompt
        names = [s["name"] for s in screenshots]
        prompt += f"\n\nScreenshots provided ({len(screenshots)}): {', '.join(names)}"

        images = [
            {"bytes": s["bytes"], "mime_type": s.get("mime_type", "image/png")}
            for s in screenshots
        ]

        text, _, _ = self._vision.get_text_with_images(
            text_prompt=prompt,
            images=images,
            schema=EVALUATE_SCHEMA,
            response_format=ResponseFormat.JSON_SCHEMA,
        )

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            _log(self._on_status, f"UX Designer: failed to parse evaluation JSON: {text[:200]}")
            return {"overall_score": 5, "summary": "Evaluation parse failed", "issues": []}

    def _apply_fixes(
        self,
        issues: List[Dict],
        service: ServiceDefinition,
        workspace: "BaseWorkspace",
        design_system: str,
    ) -> int:
        """Dispatch Coder to apply UX fixes. Returns number of fixes applied."""
        if not issues:
            return 0

        # Build issues block for the coder prompt
        issues_block = []
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "minor")
            cat = issue.get("category", "unknown")
            desc = issue.get("description", "")
            fix = issue.get("fix_description", "")
            target = issue.get("target_file", "")
            issues_block.append(
                f"{i}. [{sev.upper()}] ({cat}) {desc}\n"
                f"   Fix: {fix}\n"
                f"   File: {target or '(determine from context)'}"
            )

        fix_prompt = FIX_PROMPT_TEMPLATE.format(
            framework=service.framework,
            issues_block="\n\n".join(issues_block),
            design_system=design_system,
        )

        try:
            coder = self._coder_factory(workspace)
            # Build target files from issues (paths only). v1 Coder
            # took {"filepath", "action"} dicts; the v2 Coder/
            # ClaudeCliCoder ``code_issue`` interface takes a list of
            # path strings on the Issue.
            target_files: List[str] = []
            seen = set()
            for issue in issues:
                tf = issue.get("target_file")
                if tf and tf not in seen:
                    target_files.append(tf)
                    seen.add(tf)
            if not target_files:
                # Fallback: common CSS/component entry points
                target_files = ["src/App.tsx"]

            from bizniz.coder.types import Issue as CoderIssue
            from bizniz.quality_engineer.types import EnrichedSpec

            synthetic_issue = CoderIssue(
                id=f"UX-{service.name}-fix",
                title=f"UX fixes for {service.name}",
                description=fix_prompt,
                service=service.name,
                language=service.language or "typescript",
                target_files=target_files,
                # No test files — UX fixes are visual; verification
                # happens in the next UX iteration's screenshot eval.
                test_files=[],
            )
            # code_issue() requires architecture + enriched_spec. We
            # only have what's local to UXDesigner — pass minimal
            # stubs. The ClaudeCliCoder doesn't lean on them heavily
            # (system prompt + user prompt is what drives the model).
            minimal_arch = SystemArchitecture(
                project_name=service.name,
                project_slug=service.name,
                description="",
                services=[service],
            )
            minimal_spec = EnrichedSpec(
                problem_statement=fix_prompt[:500],
                milestone_name="ux_review",
                milestone_description="UX review fixes",
                capabilities=[],
            )
            result = coder.code_issue(
                issue=synthetic_issue,
                architecture=minimal_arch,
                enriched_spec=minimal_spec,
            )
            # CoderResult: count target files actually written.
            return len(getattr(result, "target_files_written", []) or [])
        except Exception as e:
            _log(self._on_status, f"UX Designer: coder fix failed: {e}")
            return 0

    @staticmethod
    def _login_source_hint(workspace: "BaseWorkspace") -> str:
        """Find the login page source and embed it so the AI uses real selectors.

        Without this, the AI guesses common attribute names (name="email",
        name="password") which often don't match the actual form.
        """
        workspace_root = Path(workspace.root)
        candidates = [
            "src/pages/LoginPage.tsx",
            "src/pages/Login.tsx",
            "src/routes/login.tsx",
            "src/views/Login.vue",
            "src/components/auth/LoginPage.tsx",
            "src/app/login/page.tsx",
        ]
        for rel in candidates:
            p = workspace_root / rel
            if p.exists():
                try:
                    body = p.read_text(encoding="utf-8")[:3000]
                    return (
                        f"Login form source ({rel}) — use these EXACT field "
                        f"shapes when filling the login form (placeholder, type, "
                        f"name, etc.):\n\n```tsx\n{body}\n```"
                    )
                except Exception:
                    continue
        return (
            "Login source not found at common paths — fall back to position-based "
            "locators (first text/email input, first password input)."
        )

    @staticmethod
    def _discover_routes(workspace: "BaseWorkspace", service: ServiceDefinition) -> str:
        """Try to discover routes from the frontend workspace."""
        workspace_root = Path(workspace.root)
        route_files = []

        # React: src/routes/*.tsx, src/App.tsx (React Router)
        for pattern in ["src/routes/*.tsx", "src/routes/*.jsx", "src/pages/*.tsx", "src/pages/*.jsx"]:
            route_files.extend(workspace_root.glob(pattern))

        if route_files:
            names = [f.stem for f in route_files]
            return "Discovered route files:\n" + "\n".join(f"  - {n}" for n in names)

        # Try to find route definitions in App.tsx
        app_file = workspace_root / "src" / "App.tsx"
        if not app_file.exists():
            app_file = workspace_root / "src" / "App.jsx"
        if app_file.exists():
            content = app_file.read_text()
            import re
            paths = re.findall(r'path[=:]\s*["\']([^"\']+)["\']', content)
            if paths:
                return "Routes found in App:\n" + "\n".join(f"  - {p}" for p in paths)

        return "No routes discovered — screenshot the home page and any visible navigation links."


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from AI output."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def run_ux_review(
    architecture: SystemArchitecture,
    service_workspaces: Dict[str, "BaseWorkspace"],
    compose_path: str,
    problem_statement: str,
    vision_client: GeminiClient,
    coder_factory: Optional[Callable] = None,
    on_status: Optional[Callable[[str], None]] = None,
    milestone_scope: str = "",
    max_fix_iterations: int = 2,
    acceptable_score: int = ACCEPTABLE_SCORE,
) -> List[Dict]:
    """Run UX review for all frontend services in the architecture.

    This is the top-level function called by the pipeline (Architect).
    """
    frontends = [
        s for s in architecture.services
        if s.service_type == "frontend" and s.port
    ]

    if not frontends:
        _log(on_status, "UX Designer: no frontend services, skipping")
        return []

    designer = UXDesigner(
        vision_client=vision_client,
        coder_factory=coder_factory,
        on_status=on_status,
        max_fix_iterations=max_fix_iterations,
        acceptable_score=acceptable_score,
    )

    # Derive auth contract + backend URL once. Both are optional — apps
    # without auth or backend simply don't get them.
    auth_contract = _load_auth_contract_for_compose(compose_path)
    if auth_contract:
        _log(on_status, "UX Designer: AUTH_CONTRACT.md found — auth login will be attempted")

    backend = next(
        (s for s in architecture.services if s.service_type == "backend" and s.port),
        None,
    )
    backend_url = f"http://{backend.name}:{backend.port}" if backend else None

    results = []
    for frontend in frontends:
        ws = service_workspaces.get(frontend.name)
        if ws is None:
            _log(on_status, f"UX Designer: '{frontend.name}' has no workspace, skipping")
            continue

        # Detect design system from framework/skeleton
        design_system = _detect_design_system(frontend)

        review = designer.review_frontend(
            service=frontend,
            workspace=ws,
            compose_path=compose_path,
            problem_statement=problem_statement,
            milestone_scope=milestone_scope,
            design_system=design_system,
            auth_contract=auth_contract,
            backend_url=backend_url,
        )
        results.append(review)

    return results


def _load_auth_contract_for_compose(compose_path: str) -> Optional[str]:
    """Read AUTH_CONTRACT.md from the project root if present.

    Project root is two levels up from infra/development/docker-compose.yml.
    """
    try:
        candidate = Path(compose_path).parent.parent.parent / "AUTH_CONTRACT.md"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    except Exception:
        pass
    return None


def _detect_design_system(service: ServiceDefinition) -> str:
    """Infer the design system from the service's framework."""
    fw = (service.framework or "").lower()
    if "react" in fw:
        return "Tailwind CSS v4"
    if "angular" in fw:
        return "Angular Material"
    if "vue" in fw or "nuxt" in fw:
        return "Tailwind CSS"
    if "svelte" in fw:
        return "Tailwind CSS"
    return "CSS"
