"""ProUXDesigner — three-step UX review pass.

Flow:

  1. **Code review** — Claude reads the frontend workspace (no
     screenshots) and emits a design plan: app type, palette,
     typography, primitives to build, per-view notes.

  2. **Apply global design** — Coder pass writes the design tokens
     (tailwind.config, index.css) + primitive components and adopts
     them in existing pages. Verifies Tailwind is actually wired
     into the build (the failure ClaudeUXDesigner v1 surfaced but
     couldn't fix in one shot).

  3. **Home page screenshot loop** — capture ``/`` only, eval
     against the design plan, dispatch fixes, re-capture. Stops at
     score >= acceptable_score OR iter cap. Before/after PNG pairs
     are kept on disk under ``screenshots/iter_N/home.png``.

Stops after the home page is done. Other routes (and the full
multi-route iteration) come in a follow-up phase.

Inherits the Playwright sidecar plumbing from UXDesigner.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bizniz.architect.types import ServiceDefinition
from bizniz.ux_designer.claude_ux_designer import ClaudeUXDesigner
from bizniz.ux_designer.ux_designer import _log, _strip_code_fences
from bizniz.ux_designer.v2_prompts import (
    BUILD_CHAIN_FIX_SYSTEM_PROMPT,
    BUILD_CHAIN_FIX_USER_TEMPLATE,
    CODE_REVIEW_SYSTEM_PROMPT,
    CODE_REVIEW_USER_TEMPLATE,
    GLOBAL_DESIGN_FIX_SYSTEM_PROMPT,
    GLOBAL_DESIGN_FIX_USER_TEMPLATE,
    HOME_PAGE_EVAL_SYSTEM_PROMPT,
    HOME_PAGE_EVAL_USER_TEMPLATE,
    HOME_PAGE_FIX_SYSTEM_PROMPT,
    HOME_PAGE_FIX_USER_TEMPLATE,
)


_DEFAULT_TIMEOUT_S = 1800.0
_ALLOWED_TOOLS_REVIEW = ["Read", "Glob", "Grep"]
_ALLOWED_TOOLS_VISION = ["Read", "Glob", "Grep"]


class ProUXDesigner(ClaudeUXDesigner):
    """Three-step professional UX review. See module docstring."""

    def __init__(
        self,
        vision_client,
        coder_factory: Optional[Callable] = None,
        on_status: Optional[Callable[[str], None]] = None,
        max_home_iterations: int = 2,
        acceptable_score: int = 7,
        command: str = "claude",
        timeout_seconds: int = int(_DEFAULT_TIMEOUT_S),
        additional_args: Optional[List[str]] = None,
    ):
        super().__init__(
            vision_client=vision_client,
            coder_factory=coder_factory,
            on_status=on_status,
            max_fix_iterations=max_home_iterations,
            acceptable_score=acceptable_score,
            command=command,
            timeout_seconds=timeout_seconds,
            additional_args=additional_args,
        )
        self._max_home_iterations = max_home_iterations

    # ── Public entry ───────────────────────────────────────────────────

    def review_frontend(
        self,
        service: ServiceDefinition,
        workspace,
        compose_path: str,
        problem_statement: str,
        milestone_scope: str = "",
        design_system: str = "Tailwind CSS",
        routes: Optional[List[str]] = None,
        auth_contract: Optional[str] = None,
        backend_url: Optional[str] = None,
    ) -> Dict:
        result: Dict = {
            "service": service.name,
            "step": None,
            "design_plan": None,
            "global_fix_result": None,
            # views: list of {route, view_type, iterations: [...],
            # initial_score, final_score, stopped_reason} — one entry
            # per route in per_view_plan. Home is the first entry.
            "views": [],
            "initial_score": None,
            "final_score": None,
            "stopped_reason": None,
        }

        # ── Step 1: Code review → design plan ─────────────────────────
        _log(self._on_status, f"ProUXDesigner: code review for '{service.name}'...")
        result["step"] = "code_review"
        plan = self._code_review(
            service=service, workspace=workspace,
            problem_statement=problem_statement,
        )
        result["design_plan"] = plan
        if not plan or "design_system" not in plan:
            result["stopped_reason"] = "code review returned no usable plan"
            return result
        _log(
            self._on_status,
            f"ProUXDesigner: app_type={plan.get('app_type')} — "
            f"{plan.get('summary', '')[:140]}"
        )

        # ── Step 2: Apply global design (code only) ───────────────────
        _log(self._on_status, f"ProUXDesigner: applying global design system...")
        result["step"] = "global_design"
        global_fix = self._apply_global_design(
            plan=plan, service=service, workspace=workspace,
        )
        result["global_fix_result"] = global_fix
        _log(
            self._on_status,
            f"ProUXDesigner: global design — status={global_fix.get('status')} "
            f"({len(global_fix.get('files_written', []))} files, "
            f"tailwind_wired={global_fix.get('tailwind_wired')})"
        )

        # ── Step 2.5: Verify Tailwind is actually serving ─────────────
        # The global step writes the config; this gate verifies the
        # browser actually gets the compiled CSS. Without it, the home
        # loop ends up fixing utility-class names against a stylesheet
        # that never reaches the page (recipe_box round 1: 41 files
        # written, ``tailwind_wired=True`` in the model's report, but
        # rendered home was a 1/10 unstyled wall of SVGs).
        _log(self._on_status, f"ProUXDesigner: verifying CSS is served...")
        result["step"] = "verify_css"
        css_check = self._verify_tailwind_serving(
            service=service, compose_path=compose_path,
        )
        result["css_check"] = css_check
        _log(
            self._on_status,
            f"ProUXDesigner: css gate — ok={css_check.get('ok')} "
            f"detail={css_check.get('detail', '')[:140]}"
        )
        if not css_check.get("ok"):
            _log(
                self._on_status,
                f"ProUXDesigner: CSS not serving — running build-chain fix..."
            )
            result["step"] = "build_chain_fix"
            build_fix = self._fix_build_chain(
                plan=plan, service=service, workspace=workspace,
                problem_detail=css_check.get("detail", ""),
            )
            result["build_chain_fix"] = build_fix
            _log(
                self._on_status,
                f"ProUXDesigner: build-chain fix — status={build_fix.get('status')} "
                f"root_cause={build_fix.get('root_cause', '')[:120]}"
            )
            # Re-verify.
            css_recheck = self._verify_tailwind_serving(
                service=service, compose_path=compose_path,
            )
            result["css_recheck"] = css_recheck
            _log(
                self._on_status,
                f"ProUXDesigner: css recheck — ok={css_recheck.get('ok')} "
                f"detail={css_recheck.get('detail', '')[:140]}"
            )
            if not css_recheck.get("ok"):
                result["stopped_reason"] = (
                    "tailwind still not serving after build-chain fix"
                )
                return result

        # ── Step 3: Per-view screenshot loops ─────────────────────────
        if self._coder_factory is None:
            result["stopped_reason"] = "no coder_factory for per-view loop"
            return result

        # Build the canonical route list. Prefer deterministic
        # discovery (Tier 1 parser → Tier 2 agent fallback) over the
        # LLM's per_view_plan because it doesn't hallucinate or miss
        # routes. Then merge each discovered route with the matching
        # per_view_plan entry to keep the design direction notes.
        from bizniz.ux_designer.route_discovery import (
            discover_routes_with_fallback,
        )
        from pathlib import Path as _Path
        per_view_plan = plan.get("per_view_plan") or []
        meta_by_route = {
            (v.get("route") or ""): v for v in per_view_plan
        }
        discovered = discover_routes_with_fallback(
            _Path(workspace.root),
            framework=service.framework or "react",
            on_status=self._on_status,
        )
        if discovered:
            _log(
                self._on_status,
                f"ProUXDesigner: route discovery — {len(discovered)} "
                f"route(s) from source: "
                f"{', '.join(r.path for r in discovered[:6])}"
                + ("..." if len(discovered) > 6 else "")
            )
            # Map each discovered route into a view_meta dict, pulling
            # the design direction from the plan when available. One
            # capture per dynamic route pattern — Strategy B (API seed)
            # is the script generator's job, not ours here.
            views_to_iterate = []
            for r in discovered:
                meta = meta_by_route.get(r.path) or {
                    "route": r.path,
                    "view_type": "list" if not r.is_dynamic else "detail",
                    "design_direction": (
                        f"(no design plan entry for {r.path}; "
                        f"apply the global system and use sensible "
                        f"defaults for a {('detail' if r.is_dynamic else 'list')} view)"
                    ),
                    "current_problems_from_code": "(not in design plan)",
                }
                meta = {**meta, "_is_dynamic": r.is_dynamic,
                        "_params": r.params}
                views_to_iterate.append(meta)
        elif per_view_plan:
            _log(
                self._on_status,
                "ProUXDesigner: deterministic route discovery returned "
                "nothing — falling back to design plan's per_view_plan"
            )
            views_to_iterate = list(per_view_plan)
        else:
            result["stopped_reason"] = (
                "no routes found via discovery and no per_view_plan"
            )
            return result

        _log(
            self._on_status,
            f"ProUXDesigner: per-view loop starting "
            f"({len(views_to_iterate)} routes)..."
        )
        result["step"] = "per_view_loop"
        result["route_count"] = len(views_to_iterate)
        for view_idx, view_meta in enumerate(views_to_iterate):
            route = view_meta.get("route", "/")
            view_type = view_meta.get("view_type", "")
            _log(
                self._on_status,
                f"ProUXDesigner: view {view_idx + 1}/{len(views_to_iterate)} — "
                f"route={route} type={view_type}"
            )
            view_result: Dict = {
                "route": route,
                "view_type": view_type,
                "iterations": [],
                "initial_score": None,
                "final_score": None,
                "stopped_reason": None,
            }
            for iteration in range(1, self._max_home_iterations + 1):
                iter_result = self._view_iteration(
                    route=route,
                    view_meta=view_meta,
                    view_label=self._safe_view_label(route),
                    iteration=iteration,
                    service=service, workspace=workspace,
                    compose_path=compose_path,
                    problem_statement=problem_statement,
                    milestone_scope=milestone_scope,
                    design_plan=plan,
                    auth_contract=auth_contract,
                    backend_url=backend_url,
                )
                view_result["iterations"].append(iter_result)
                if (
                    view_result["initial_score"] is None
                    and iter_result.get("initial_score") is not None
                ):
                    view_result["initial_score"] = iter_result["initial_score"]
                if iter_result.get("final_score") is not None:
                    view_result["final_score"] = iter_result["final_score"]
                if iter_result.get("stop"):
                    view_result["stopped_reason"] = iter_result.get(
                        "stop_reason", "stop_recommendation",
                    )
                    break
            else:
                view_result["stopped_reason"] = "iter cap reached"
            result["views"].append(view_result)
            # Track the home page's score on the top-level result for
            # quick "did we improve" reporting.
            if route == "/" and result["initial_score"] is None:
                result["initial_score"] = view_result["initial_score"]
                result["final_score"] = view_result["final_score"]

        result["stopped_reason"] = "all views iterated"
        return result

    @staticmethod
    def _safe_view_label(route: str) -> str:
        """A filesystem-safe label for a route. ``/`` → ``home``,
        ``/recipes/:id`` → ``recipes_id``. Used to namespace
        per-iteration screenshot dirs and raw-response dumps."""
        if not route or route == "/":
            return "home"
        return (
            route.strip("/")
            .replace("/", "_")
            .replace(":", "")
            .replace(".", "_")
            or "home"
        )

    # ── Step 1: Code review ────────────────────────────────────────────

    def _code_review(
        self,
        service: ServiceDefinition,
        workspace,
        problem_statement: str,
    ) -> Optional[Dict]:
        ws_root = Path(workspace.root)
        user_prompt = CODE_REVIEW_USER_TEMPLATE.format(
            project_name=getattr(service, "project_name", service.name),
            problem_statement=problem_statement,
            framework=service.framework or "react",
            language=service.language or "typescript",
        )
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", CODE_REVIEW_SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS_REVIEW),
            "--add-dir", str(ws_root),
        ] + self._additional_args
        return self._invoke_and_parse(cmd, user_prompt, ws_root, label="code_review")

    # ── Step 2: Global design (Coder dispatch) ─────────────────────────

    def _apply_global_design(
        self,
        plan: Dict,
        service: ServiceDefinition,
        workspace,
    ) -> Dict:
        ws_root = Path(workspace.root)
        sys_prompt = GLOBAL_DESIGN_FIX_SYSTEM_PROMPT.format(
            framework=service.framework or "react",
        )
        user_prompt = GLOBAL_DESIGN_FIX_USER_TEMPLATE.format(
            design_plan_json=json.dumps(plan, indent=2),
        )
        # Use full Coder toolset (Edit/Write/Read/Bash/Glob/Grep) so
        # Claude can actually modify files.
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", sys_prompt,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Edit Write Read Bash Glob Grep",
            "--add-dir", str(ws_root),
        ] + self._additional_args
        out = self._invoke_and_parse(cmd, user_prompt, ws_root, label="global_design")
        return out or {
            "status": "failed",
            "files_written": [],
            "tailwind_wired": False,
            "summary": "global_design step returned no JSON",
            "notes": [],
        }

    # ── Step 3: Per-view iteration ─────────────────────────────────────

    def _view_iteration(
        self,
        route: str,
        view_meta: Dict,
        view_label: str,
        iteration: int,
        service: ServiceDefinition,
        workspace,
        compose_path: str,
        problem_statement: str,
        milestone_scope: str,
        design_plan: Dict,
        auth_contract: Optional[str],
        backend_url: Optional[str],
    ) -> Dict:
        """One screenshot → eval → fix cycle for a single route.

        ``view_label`` is a filesystem-safe slug (e.g. ``home``,
        ``recipes_id_edit``) used to namespace per-iteration dirs and
        raw-response dumps so screenshots from /login don't overwrite
        screenshots from /dashboard."""
        _log(
            self._on_status,
            f"ProUXDesigner: {view_label} iter {iteration} — capturing..."
        )
        screenshots = self._take_view_screenshots(
            route=route,
            view_label=view_label,
            iteration=iteration,
            service=service, workspace=workspace,
            compose_path=compose_path,
            problem_statement=problem_statement,
            milestone_scope=milestone_scope,
            auth_contract=auth_contract,
            backend_url=backend_url,
        )

        if not screenshots:
            return {
                "iteration": iteration,
                "screenshots": [],
                "evaluation": None,
                "fix_result": None,
                "initial_score": None,
                "final_score": None,
                "stop": True,
                "stop_reason": "no screenshots captured",
            }

        _log(
            self._on_status,
            f"ProUXDesigner: {view_label} iter {iteration} — evaluating..."
        )
        evaluation = self._evaluate_view(
            screenshots=screenshots,
            route=route,
            view_meta=view_meta,
            view_label=view_label,
            iteration=iteration,
            design_plan=design_plan,
            service=service,
        )
        score = evaluation.get("overall_score", 0)
        issues = evaluation.get("issues", []) or []
        stop_rec = (evaluation.get("stop_recommendation") or "").lower()
        _log(
            self._on_status,
            f"ProUXDesigner: {view_label} iter {iteration} — score={score}/10, "
            f"{len(issues)} issue(s), stop_rec={stop_rec or 'n/a'}"
        )

        out = {
            "iteration": iteration,
            "screenshots": [str(s["path"]) for s in screenshots],
            "evaluation": evaluation,
            "fix_result": None,
            "initial_score": score,
            "final_score": score,
            "stop": False,
            "stop_reason": None,
        }

        if stop_rec == "stop" or score >= self._acceptable_score:
            out["stop"] = True
            out["stop_reason"] = (
                f"score {score} >= acceptable {self._acceptable_score}"
                if score >= self._acceptable_score
                else "vision recommended stop"
            )
            return out
        if not issues:
            out["stop"] = True
            out["stop_reason"] = "no actionable issues despite low score"
            return out

        # Apply fixes through Coder (factory path same as base class).
        fix_result = self._apply_view_fixes(
            issues=issues,
            view_meta=view_meta,
            view_label=view_label,
            design_plan=design_plan,
            service=service, workspace=workspace,
        )
        out["fix_result"] = fix_result
        _log(
            self._on_status,
            f"ProUXDesigner: {view_label} iter {iteration} — applied "
            f"{len(fix_result.get('files_written', []))} file change(s)"
        )
        return out

    # ── Helpers ────────────────────────────────────────────────────────

    def _take_view_screenshots(
        self,
        route: str,
        view_label: str,
        iteration: int,
        service: ServiceDefinition,
        workspace,
        compose_path: str,
        problem_statement: str,
        milestone_scope: str,
        auth_contract: Optional[str],
        backend_url: Optional[str],
    ) -> List[Dict]:
        """Capture screenshots for a single route and copy them into a
        per-view per-iteration dir so before/after pairs survive across
        the multi-route loop."""
        shots = self._take_screenshots(
            service=service,
            workspace=workspace,
            compose_path=compose_path,
            problem_statement=problem_statement,
            milestone_scope=milestone_scope,
            routes=[route],
            auth_contract=auth_contract,
            backend_url=backend_url,
        )
        if not shots:
            return shots

        ws_root = Path(workspace.root)
        iter_dir = ws_root / "screenshots" / view_label / f"iter_{iteration}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        copied = []
        for s in shots:
            src = Path(s["path"])
            if not src.exists():
                continue
            dest = iter_dir / src.name
            try:
                shutil.copy2(src, dest)
            except Exception:
                continue
            copied.append({
                "name": s.get("name", src.stem),
                "path": dest,
                "bytes": s.get("bytes", b""),
            })
        return copied or shots

    def _evaluate_view(
        self,
        screenshots: List[Dict],
        route: str,
        view_meta: Dict,
        view_label: str,
        iteration: int,
        design_plan: Dict,
        service: ServiceDefinition,
    ) -> Dict:
        if not screenshots:
            return {
                "overall_score": 0,
                "matches_spec": False,
                "deltas_from_spec": [],
                "issues": [],
                "summary": "no screenshots",
                "stop_recommendation": "stop",
            }
        first_path = Path(screenshots[0]["path"])
        screenshots_dir = first_path.parent
        plan_summary = self._design_plan_summary(design_plan)
        # Augment the plan summary with the per-view direction so the
        # eval is scoring against the specific spec for this route.
        view_section = self._render_view_meta(route, view_meta)
        full_summary = f"{plan_summary}\n\n{view_section}"
        user_prompt = HOME_PAGE_EVAL_USER_TEMPLATE.format(
            iteration=iteration,
            design_plan_summary=full_summary,
            app_type=design_plan.get("app_type", "hybrid"),
            framework=service.framework or "react",
        )
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", HOME_PAGE_EVAL_SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", " ".join(_ALLOWED_TOOLS_VISION),
            "--add-dir", str(screenshots_dir),
        ] + self._additional_args
        parsed = self._invoke_and_parse(
            cmd, user_prompt, screenshots_dir,
            label=f"{view_label}_eval_iter{iteration}",
        )
        if parsed is None:
            return {
                "overall_score": 5,
                "matches_spec": False,
                "deltas_from_spec": [],
                "issues": [],
                "summary": "eval returned no parseable JSON",
                "stop_recommendation": "stop",
            }
        return parsed

    @staticmethod
    def _render_view_meta(route: str, view_meta: Dict) -> str:
        """Compact the view-specific design direction for the eval prompt."""
        return (
            f"VIEW UNDER REVIEW:\n"
            f"  route: {route}\n"
            f"  view_type: {view_meta.get('view_type', '?')}\n"
            f"  current_problems_from_code: "
            f"{view_meta.get('current_problems_from_code', '')[:400]}\n"
            f"  design_direction: "
            f"{view_meta.get('design_direction', '')[:600]}"
        )

    def _apply_view_fixes(
        self,
        issues: List[Dict],
        view_meta: Dict,
        view_label: str,
        design_plan: Dict,
        service: ServiceDefinition,
        workspace,
    ) -> Dict:
        ws_root = Path(workspace.root)
        sys_prompt = HOME_PAGE_FIX_SYSTEM_PROMPT.format(
            framework=service.framework or "react",
        )
        issues_block = []
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "minor")
            desc = issue.get("description", "")
            fix = issue.get("fix_description", "")
            target = issue.get("target_file") or "(determine from context)"
            issues_block.append(
                f"{i}. [{sev.upper()}] {desc}\n"
                f"   Fix: {fix}\n"
                f"   File: {target}"
            )
        full_summary = (
            self._design_plan_summary(design_plan)
            + "\n\n"
            + self._render_view_meta(view_meta.get("route", "/"), view_meta)
        )
        user_prompt = HOME_PAGE_FIX_USER_TEMPLATE.format(
            issues_block="\n\n".join(issues_block),
            design_plan_summary=full_summary,
        )
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", sys_prompt,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Edit Write Read Bash Glob Grep",
            "--add-dir", str(ws_root),
        ] + self._additional_args
        return self._invoke_and_parse(
            cmd, user_prompt, ws_root, label=f"{view_label}_fix",
        ) or {
            "status": "failed", "files_written": [],
            "summary": "fix step returned no JSON", "notes": [],
        }

    def _design_plan_summary(self, plan: Dict) -> str:
        """Compact the design plan for downstream prompts so each
        Claude session doesn't re-read the full JSON every time."""
        ds = plan.get("design_system", {}) or {}
        palette = ds.get("palette", {}) or {}
        prims = plan.get("primitives_to_build", []) or []
        return (
            f"app_type: {plan.get('app_type')}\n"
            f"summary: {plan.get('summary', '')}\n"
            f"palette primary: {palette.get('primary')} | "
            f"background: {palette.get('background')} | "
            f"surface: {palette.get('surface')}\n"
            f"font sans: {(ds.get('typography', {}) or {}).get('font_family_sans')}\n"
            f"primitives: {', '.join(p.get('name', '') for p in prims)}"
        )

    # ── Step 2.5 helpers: CSS serving gate + build-chain fix ──────────

    def _verify_tailwind_serving(
        self,
        service: ServiceDefinition,
        compose_path: str,
    ) -> Dict:
        """Fetch the rendered HTML from the frontend dev server and
        check whether linked stylesheets actually contain Tailwind
        output. Returns ``{ok: bool, detail: str, css_urls: [...]}``.
        Best-effort and resilient to docker-network DNS quirks — when
        we can't probe at all, returns ``ok=False`` with a reason so
        the caller can decide whether to push on or stop."""
        import urllib.request
        import urllib.error
        try:
            from bizniz.integration.contracts import _resolve_host_port
            host_port = _resolve_host_port(
                compose_path, service.name, service.port,
            )
        except Exception:
            host_port = service.port
        base = f"http://localhost:{host_port}"
        try:
            with urllib.request.urlopen(base + "/", timeout=10) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            return {
                "ok": False,
                "detail": f"frontend / unreachable at {base}: {type(e).__name__}: {e}",
                "css_urls": [],
            }
        # Find stylesheet refs + inline <style> blocks. Vite dev mode
        # injects styles via JS modules too — also probe /src/index.css
        # and /index.css as direct hits since those are the canonical
        # entry points we wrote.
        css_urls = []
        for m in re.finditer(
            r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        ):
            css_urls.append(m.group(1))
        # Vite-style direct module hits.
        for guess in ("/src/index.css", "/src/styles/index.css",
                      "/src/main.css", "/index.css"):
            if guess not in css_urls:
                css_urls.append(guess)

        body_blobs = []
        for url in list(css_urls):
            full = url if url.startswith("http") else base + url
            try:
                with urllib.request.urlopen(full, timeout=10) as r:
                    body_blobs.append((url, r.read().decode("utf-8", errors="replace")))
            except Exception:
                continue

        # Also include inline <style> bodies — vite dev mode often
        # injects CSS this way.
        for m in re.finditer(
            r"<style[^>]*>(.*?)</style>", html, re.DOTALL | re.IGNORECASE,
        ):
            body_blobs.append(("<inline>", m.group(1)))

        # Tailwind signatures: --tw-* custom properties (most reliable),
        # or telltale utility class definitions.
        tailwind_markers = ("--tw-", "\\.bg-", "\\.text-", "\\.flex", "tailwindcss")
        hits = []
        for url, body in body_blobs:
            if any(re.search(m, body) for m in tailwind_markers):
                hits.append(url)
        if hits:
            return {
                "ok": True,
                "detail": f"tailwind output detected in {hits[:3]}",
                "css_urls": css_urls,
            }
        return {
            "ok": False,
            "detail": (
                f"no tailwind utility markers found in "
                f"{len(body_blobs)} served stylesheet(s). probed: "
                f"{css_urls[:5]}"
            ),
            "css_urls": css_urls,
        }

    def _fix_build_chain(
        self,
        plan: Dict,
        service: ServiceDefinition,
        workspace,
        problem_detail: str,
    ) -> Dict:
        ws_root = Path(workspace.root)
        sys_prompt = BUILD_CHAIN_FIX_SYSTEM_PROMPT.format(
            framework=service.framework or "react",
        )
        user_prompt = BUILD_CHAIN_FIX_USER_TEMPLATE.format(
            design_plan_summary=self._design_plan_summary(plan),
            problem_detail=problem_detail,
        )
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", sys_prompt,
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Edit Write Read Bash Glob Grep",
            "--add-dir", str(ws_root),
        ] + self._additional_args
        return self._invoke_and_parse(
            cmd, user_prompt, ws_root, label="build_chain_fix",
        ) or {
            "status": "failed", "root_cause": "no JSON returned",
            "files_written": [], "summary": "build chain fix returned no JSON",
            "notes": [],
        }

    def _invoke_and_parse(
        self,
        cmd: List[str],
        user_prompt: str,
        cwd: Path,
        label: str,
    ) -> Optional[Dict]:
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, input=user_prompt,
                capture_output=True, text=True,
                timeout=self._timeout_s,
                cwd=str(cwd),
            )
        except subprocess.TimeoutExpired:
            _log(
                self._on_status,
                f"ProUXDesigner.{label}: timed out after {self._timeout_s:.0f}s",
            )
            return None
        except FileNotFoundError as e:
            _log(self._on_status, f"ProUXDesigner.{label}: claude missing: {e}")
            return None
        elapsed = time.time() - t0
        _log(
            self._on_status,
            f"ProUXDesigner.{label}: subprocess done in {elapsed:.1f}s "
            f"(exit {proc.returncode})"
        )
        if proc.returncode != 0:
            _log(
                self._on_status,
                f"ProUXDesigner.{label}: exit {proc.returncode}; "
                f"stderr tail: {(proc.stderr or '')[-200:]}",
            )
            return None
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            _log(
                self._on_status,
                f"ProUXDesigner.{label}: non-JSON CLI output: "
                f"{proc.stdout[:200]}",
            )
            return None
        if payload.get("is_error"):
            _log(
                self._on_status,
                f"ProUXDesigner.{label}: is_error=true",
            )
            return None
        result_text = payload.get("result") or ""
        # Always-on raw dump so we can compare Claude's output to
        # what the parser extracted (helps diagnose JSON shape
        # mismatches even when parsing nominally succeeds).
        try:
            (cwd / f"._proux_{label}_raw.txt").write_text(result_text)
        except Exception:
            pass
        parsed = self._parse_eval_json(result_text)
        if parsed is None:
            _log(
                self._on_status,
                f"ProUXDesigner.{label}: unparseable response "
                f"(first 300 chars): {result_text[:300]}"
            )
        return parsed
