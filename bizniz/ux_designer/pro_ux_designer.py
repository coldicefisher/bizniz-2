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
        review_store=None,
        project_slug: Optional[str] = None,
        debug: bool = False,
        # Design system lock (sub-ticket of roadmap item 2). When a
        # design_lock.json exists in the workspace, code_review +
        # apply_global_design are skipped — the established design
        # is reused. Set ``force_redesign=True`` to ignore + replace
        # the lock; useful for explicit "redesign milestone" runs.
        force_redesign: bool = False,
        # Roadmap item 2 done-when criterion — per-story Storybook
        # loop alongside the per-route loop. Default off until
        # live-validated on a real Storybook server.
        storybook_driver: Optional["StorybookDriver"] = None,
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
        # Optional review cache. When wired, ProUXDesigner consults
        # the store before iterating a route and skips it if the
        # cached score is acceptable AND no dirty signal fires.
        # ``project_slug`` keys the per-project rows.
        self._review_store = review_store
        self._project_slug = project_slug
        self._debug = debug
        self._force_redesign = force_redesign
        self._storybook_driver = storybook_driver
        # Per-run timing. Populated by ``_timed`` calls and surfaced
        # on the ``result["timing"]`` dict at the end of
        # review_frontend.
        self._timings: Dict[str, float] = {}

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
        # Reset per-run timing for this invocation.
        self._timings = {}
        _t_run_start = time.time()

        # Surface the trend across recent runs at start so the
        # operator can tell at a glance whether things are getting
        # faster + scores climbing.
        from bizniz.ux_designer import run_log
        from pathlib import Path as _PathRL
        ws_root_rl = _PathRL(workspace.root)
        try:
            recent = run_log.recent_summaries(ws_root_rl, n=5)
            if recent:
                _log(
                    self._on_status,
                    f"ProUXDesigner: prior runs — "
                    f"{run_log.format_trend(recent)}"
                )
        except Exception:
            pass
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
            "timing": {},  # populated at exit
        }

        # ── Plan cache check (item #4) ──────────────────────────────
        # Skip code_review + global_design when nothing has changed
        # since the prior run. Recipe_box v2.7 burned 440s on these
        # two phases despite emitting essentially the same plan as
        # the prior run.
        from bizniz.ux_designer import plan_cache
        from pathlib import Path as _Path
        ws_root_path = _Path(workspace.root)
        # ── Design system lock (sub-ticket of roadmap item 2) ──
        # If a design_lock.json exists, the design system was
        # established on a prior milestone — skip code_review +
        # apply_global_design entirely. Without this, every milestone
        # whose plan_cache misses (i.e. every milestone, because
        # IMPLEMENT legitimately writes new files) would re-derive
        # the palette + typography + primitives. Cost: ~15 min per
        # milestone wasted + visual jitter as the model drifts hex
        # values between runs.
        #
        # ``force_redesign=True`` (constructor) ignores + replaces
        # the lock — for explicit "redesign milestone" runs.
        from bizniz.ux_designer import design_lock as _design_lock
        if self._force_redesign:
            removed = _design_lock.remove_lock(ws_root_path)
            if removed:
                _log(
                    self._on_status,
                    "ProUXDesigner: force_redesign=True — removed "
                    "existing design_lock.json; re-establishing "
                    "from scratch"
                )
        lock = _design_lock.load_lock(ws_root_path)
        plan = None
        global_fix = None
        if lock is not None:
            plan = lock.plan
            global_fix = lock.global_fix_result
            _log(
                self._on_status,
                f"ProUXDesigner: design lock HIT — reusing design "
                f"established at milestone {lock.milestone_index} on "
                f"{lock.established_at.isoformat()[:19]} "
                f"({len(lock.files_managed)} managed file(s))"
            )
            self._record_timing("code_review_locked", 0.0)
            self._record_timing("global_design_locked", 0.0)
            result["design_lock_hit"] = True
        else:
            result["design_lock_hit"] = False

        # Load the prior cache (within-milestone-resume optimization
        # — distinct from the design lock above). Only consulted when
        # the lock didn't already provide plan + global_fix. The cache
        # catches the case where an M1 run was interrupted mid-UX and
        # we resume before the lock could be saved.
        # See docs/backlog/ux_followups_2026-05-14.md (Ticket 1).
        cached_payload = None
        if plan is None:
            cached_payload = plan_cache.load_cache(ws_root_path)
            managed = plan_cache.managed_files_from_cache(cached_payload)
            cur_input_mtime = plan_cache.compute_input_mtime(
                ws_root_path, exclude_relpaths=managed,
            )
            if cached_payload is not None:
                valid, reason = plan_cache.is_cache_valid(
                    cached_payload,
                    current_input_mtime=cur_input_mtime,
                    workspace_root=ws_root_path,
                )
                if valid:
                    plan = cached_payload.get("plan")
                    global_fix = cached_payload.get("global_fix_result")
                    _log(
                        self._on_status,
                        f"ProUXDesigner: plan cache HIT — reusing prior "
                        f"plan + global-design (saved "
                        f"{cached_payload.get('saved_at', '?')[:19]})"
                    )
                    self._record_timing("code_review_cached", 0.0)
                    self._record_timing("global_design_cached", 0.0)
                else:
                    _log(
                        self._on_status,
                        f"ProUXDesigner: plan cache MISS — {reason}"
                    )

        # ── Step 1: Code review → design plan ─────────────────────────
        if plan is None:
            _log(self._on_status, f"ProUXDesigner: code review for '{service.name}'...")
            result["step"] = "code_review"
            _t0 = time.time()
            plan = self._code_review(
                service=service, workspace=workspace,
                problem_statement=problem_statement,
            )
            self._record_timing("code_review", time.time() - _t0)
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
        if global_fix is None:
            _log(self._on_status, f"ProUXDesigner: applying global design system...")
            result["step"] = "global_design"
            _t0 = time.time()
            global_fix = self._apply_global_design(
                plan=plan, service=service, workspace=workspace,
            )
            self._record_timing("global_design", time.time() - _t0)
            # Save the design lock — this is the durable record that
            # future milestones consult to skip code_review +
            # apply_global_design entirely. Sub-ticket of roadmap
            # item 2.
            try:
                files_written = list(
                    (global_fix or {}).get("files_written") or []
                )
                new_lock = _design_lock.DesignLock(
                    milestone_index=getattr(
                        service, "milestone_index", 0,
                    ),
                    plan=plan or {},
                    global_fix_result=global_fix or {},
                    files_managed=files_written,
                )
                _design_lock.save_lock(ws_root_path, new_lock)
                _log(
                    self._on_status,
                    f"ProUXDesigner: design lock saved at "
                    f"{ws_root_path}/.bizniz/design_lock.json "
                    f"({len(files_written)} files managed)"
                )
            except Exception as e:
                _log(
                    self._on_status,
                    f"ProUXDesigner: design_lock save failed "
                    f"({type(e).__name__}: {e}) — non-fatal"
                )
            # Save the within-milestone plan cache too (for resume
            # within the same milestone if interrupted before the
            # lock load on next run). ``input_mtime`` here excludes
            # the files global_design just wrote.
            try:
                fresh_managed = list(
                    (global_fix or {}).get("files_written") or []
                )
                plan_cache.save_cache(
                    ws_root_path,
                    plan=plan,
                    global_fix_result=global_fix,
                    input_mtime=plan_cache.compute_input_mtime(
                        ws_root_path, exclude_relpaths=fresh_managed,
                    ),
                )
            except Exception as e:
                _log(
                    self._on_status,
                    f"ProUXDesigner: plan_cache save failed "
                    f"({type(e).__name__}: {e}) — non-fatal"
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
        _t0 = time.time()
        css_check = self._verify_tailwind_serving(
            service=service, compose_path=compose_path,
        )
        self._record_timing("verify_css", time.time() - _t0)
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
            _t0 = time.time()
            build_fix = self._fix_build_chain(
                plan=plan, service=service, workspace=workspace,
                problem_detail=css_check.get("detail", ""),
            )
            self._record_timing("build_chain_fix", time.time() - _t0)
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
            apply_conservative_auth_default,
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
        # Fill in ``requires_auth=True`` for unknown non-public routes
        # — the safer default, since pre-authing a public route is
        # harmless while skipping auth on a protected one captures
        # the login redirect.
        if discovered:
            apply_conservative_auth_default(discovered)
            n_auth = sum(1 for r in discovered if r.requires_auth)
            _log(
                self._on_status,
                f"ProUXDesigner: auth detection — "
                f"{n_auth}/{len(discovered)} routes flagged requires_auth"
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
                meta = {
                    **meta,
                    "_is_dynamic": r.is_dynamic,
                    "_params": r.params,
                    "_requires_auth": r.requires_auth,
                    "source_file": r.source_file,
                }
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

        # ── Storybook per-story loop (roadmap item 2 done-when) ──────
        # Opt-in via constructor injection. Runs alongside the per-
        # route loop — different content target (primitives vs pages).
        # Result stashed on ``result["storybook"]``; per-route loop
        # below proceeds unchanged regardless.
        if self._storybook_driver is not None:
            _t0_sb = time.time()
            screenshots_dir = ws_root_path / ".bizniz" / "storybook_shots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            try:
                sb_result = self._storybook_driver.run(
                    frontend_root=ws_root_path,
                    screenshots_dir=screenshots_dir,
                    design_lock_json=(
                        lock.model_dump_json(indent=2)
                        if lock is not None else None
                    ),
                )
                result["storybook"] = sb_result.model_dump()
                _log(
                    self._on_status,
                    f"ProUXDesigner: storybook loop done — "
                    f"{sb_result.score.passing}/{sb_result.score.covered} "
                    f"passing (mean={sb_result.score.mean})"
                    if sb_result.skipped_reason is None
                    else f"ProUXDesigner: storybook skipped — {sb_result.skipped_reason}"
                )
            except Exception as e:
                # Defensive: never let a Storybook bug tank the
                # whole UX phase. Log and continue with per-route.
                _log(
                    self._on_status,
                    f"ProUXDesigner: storybook loop raised "
                    f"{type(e).__name__}: {e} — continuing with per-route"
                )
                result["storybook"] = {
                    "skipped_reason": (
                        f"driver raised {type(e).__name__}: {e}"
                    ),
                }
            self._record_timing("storybook", time.time() - _t0_sb)

        _log(
            self._on_status,
            f"ProUXDesigner: per-view loop starting "
            f"({len(views_to_iterate)} routes)..."
        )
        result["step"] = "per_view_loop"
        result["route_count"] = len(views_to_iterate)
        # Tee the full route list so _verify_capture can detect
        # dynamic-route → literal-sibling collisions (recipes_id →
        # recipes_new style).
        self._sibling_routes = [
            v.get("route", "") for v in views_to_iterate
        ]
        # Compute the "global styles changed since last review?" mtime
        # once per project — same value for every route this run.
        from bizniz.ux_designer.review_store import (
            ReviewRecord, ReviewStore,
            max_global_mtime, source_mtime,
        )
        from datetime import datetime as _dt
        current_globals_mtime = max_global_mtime(_Path(workspace.root))

        # ── Dynamic-route resolution ─────────────────────────────────
        # For every dynamic route (e.g. /recipes/:id), ask the resolver
        # agent for a concrete URL (e.g. /recipes/abc-123) so the
        # Playwright script can navigate directly instead of inventing
        # ids. Cached in .bizniz/ux_resolved_routes.json + validated
        # via HTTP probe each run. See route_resolver.py.
        self._resolved_url_by_template: Dict[str, str] = {}
        if discovered:
            dynamic_specs = [r for r in discovered if r.is_dynamic]
            if dynamic_specs:
                from bizniz.ux_designer.route_resolver import (
                    resolve_dynamic_routes,
                )
                openapi = self._find_openapi(_Path(workspace.root))
                _t0 = time.time()
                try:
                    resolved = resolve_dynamic_routes(
                        _Path(workspace.root),
                        discovered,
                        backend_url=backend_url,
                        openapi_path=openapi,
                        auth_contract=auth_contract,
                        on_status=self._on_status,
                    )
                    self._resolved_url_by_template = {
                        t: r.concrete_url
                        for t, r in resolved.items()
                        if r.concrete_url
                    }
                    self._record_timing("route_resolve", time.time() - _t0)
                    _log(
                        self._on_status,
                        f"ProUXDesigner: resolved "
                        f"{len(self._resolved_url_by_template)}/"
                        f"{len(dynamic_specs)} dynamic route(s) — "
                        f"{', '.join(f'{t}→{u}' for t, u in list(self._resolved_url_by_template.items())[:3])}"
                    )
                except Exception as e:
                    _log(
                        self._on_status,
                        f"ProUXDesigner: route_resolver raised "
                        f"{type(e).__name__}: {e} — falling back to "
                        f"in-script seeding"
                    )
                    self._resolved_url_by_template = {}

        # ── Pre-capture optimization ─────────────────────────────────
        # Generate ONE multi-route Playwright script + run the sidecar
        # ONCE. Per-view iter 1 reads from these cached PNGs; iter 2+
        # falls back to per-route capture (after a fix has been
        # applied). On the recipe_box validation, capture was 54% of
        # runtime (1262s/2318s) because we re-ran Playwright per route
        # per iteration. With this, capture drops to ~1 invocation
        # (~100-150s).
        self._precaptured_by_route: Dict[str, List[Dict]] = {}
        all_routes = [
            v.get("route", "") for v in views_to_iterate
            if v.get("route")
        ]
        if all_routes:
            _log(
                self._on_status,
                f"ProUXDesigner: pre-capturing all "
                f"{len(all_routes)} routes in one sidecar..."
            )
            _t0 = time.time()
            try:
                bulk_shots = self._take_screenshots(
                    service=service,
                    workspace=workspace,
                    compose_path=compose_path,
                    problem_statement=problem_statement,
                    milestone_scope=milestone_scope,
                    routes=self._translate_to_concrete(all_routes),
                    auth_contract=auth_contract,
                    backend_url=backend_url,
                )
                self._precaptured_by_route = self._rekey_to_templates(
                    self._bucket_shots_by_route(bulk_shots),
                )
                covered = sum(
                    1 for r in all_routes
                    if self._precaptured_by_route.get(r)
                )
                _log(
                    self._on_status,
                    f"ProUXDesigner: pre-capture done in "
                    f"{time.time() - _t0:.1f}s — "
                    f"{covered}/{len(all_routes)} routes covered "
                    f"({len(bulk_shots)} total shots)"
                )
                self._record_timing("pre_capture", time.time() - _t0)
            except Exception as e:
                _log(
                    self._on_status,
                    f"ProUXDesigner: pre-capture raised "
                    f"{type(e).__name__}: {e} — falling back to "
                    f"per-route capture"
                )
                self._precaptured_by_route = {}

        for view_idx, view_meta in enumerate(views_to_iterate):
            route = view_meta.get("route", "/")
            view_type = view_meta.get("view_type", "")
            source_file_rel = view_meta.get("source_file")
            current_source_mt = source_mtime(
                _Path(workspace.root), source_file_rel,
            )

            # Cache lookup. Skip the route entirely if the store has a
            # passing score and no dirty signal fires.
            cached = None
            if self._review_store is not None and self._project_slug:
                cached = self._review_store.get(self._project_slug, route)
            if cached is not None:
                dirty, reason = ReviewStore.is_dirty(
                    cached,
                    current_source_mtime=current_source_mt,
                    current_globals_mtime=current_globals_mtime,
                    acceptable_score=self._acceptable_score,
                )
                if not dirty:
                    _log(
                        self._on_status,
                        f"ProUXDesigner: view {view_idx + 1}/{len(views_to_iterate)} — "
                        f"route={route} CACHED (score={cached.last_score}, "
                        f"reviewed {cached.last_reviewed_at.isoformat()})"
                    )
                    result["views"].append({
                        "route": route,
                        "view_type": view_type,
                        "iterations": [],
                        "initial_score": cached.last_score,
                        "final_score": cached.last_score,
                        "stopped_reason": "cached (clean)",
                        "cached": True,
                    })
                    if route == "/" and result["initial_score"] is None:
                        result["initial_score"] = cached.last_score
                        result["final_score"] = cached.last_score
                    continue
                else:
                    _log(
                        self._on_status,
                        f"ProUXDesigner: view {view_idx + 1}/{len(views_to_iterate)} — "
                        f"route={route} DIRTY ({reason}); re-reviewing"
                    )

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
                "cached": False,
                # Set to True if ALL iterations had capture mismatches
                # (Ticket 2). The view is excluded from APP SCORE and
                # not persisted to review_store.
                "not_reviewable": False,
                "capture_mismatch_reason": None,
            }
            # Adaptive budget: if this route hit the iter cap last
            # time without converging, grant +2 extra iterations
            # this run. Easy routes (cached score >= threshold but
            # dirty for some other reason, or no history) stay at
            # default. Hard routes get more rope before we mark them
            # stuck for good.
            budget = self._budget_for_route(cached)
            if budget > self._max_home_iterations:
                _log(
                    self._on_status,
                    f"ProUXDesigner: route={route} bumping iter budget "
                    f"to {budget} (prior run hit cap)"
                )
            for iteration in range(1, budget + 1):
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
            # A view is not_reviewable when every iteration had a
            # capture mismatch (Ticket 2). One good capture is enough
            # to score; only "no good captures" knocks the view out
            # of the APP SCORE aggregation.
            iters = view_result["iterations"]
            if iters and all(it.get("not_reviewable") for it in iters):
                view_result["not_reviewable"] = True
                view_result["capture_mismatch_reason"] = iters[-1].get(
                    "capture_mismatch_reason"
                )
            else:
                view_result["stopped_reason"] = "iter cap reached"
            result["views"].append(view_result)
            # Track the home page's score on the top-level result for
            # quick "did we improve" reporting.
            if route == "/" and result["initial_score"] is None:
                result["initial_score"] = view_result["initial_score"]
                result["final_score"] = view_result["final_score"]

            # Persist the review result so the next run can short-
            # circuit clean routes. Iterations count = how many
            # screenshot→fix cycles were needed to converge.
            # Skip the upsert when the view was not_reviewable
            # (Ticket 2): caching a "passing 8/10" for /admin when
            # the screenshot was actually /admin/users would let the
            # next run skip /admin without ever seeing the real page.
            if (
                self._review_store is not None
                and self._project_slug
                and not view_result.get("not_reviewable")
            ):
                final_score = view_result["final_score"]
                iters_run = len(view_result["iterations"])
                try:
                    self._review_store.upsert(ReviewRecord(
                        project_slug=self._project_slug,
                        route=route,
                        view_type=view_type,
                        requires_auth=view_meta.get("_requires_auth"),
                        last_score=final_score,
                        iterations_to_acceptable=iters_run,
                        last_reviewed_at=_dt.utcnow(),
                        source_file=source_file_rel,
                        source_mtime=current_source_mt,
                        global_styles_mtime=current_globals_mtime,
                    ))
                except Exception as e:
                    _log(
                        self._on_status,
                        f"ProUXDesigner: review_store upsert failed "
                        f"for {route}: {type(e).__name__}: {e}"
                    )

        result["stopped_reason"] = "all views iterated"

        # Record timings on the result + emit a summary line.
        total = time.time() - _t_run_start
        self._record_timing("total", total)
        result["timing"] = dict(self._timings)
        _log(
            self._on_status,
            f"ProUXDesigner: timing — {self._format_timings()}",
        )
        # Compact per-view breakdown for quick scanning.
        views = result.get("views") or []
        cached_count = sum(1 for v in views if v.get("cached"))
        iterated = [v for v in views if not v.get("cached")]
        all_scores = [
            v.get("final_score") for v in views
            if v.get("final_score") is not None
        ]
        avg_score = (sum(all_scores) / len(all_scores)) if all_scores else None
        _log(
            self._on_status,
            f"ProUXDesigner: {len(views)} views — {cached_count} cached, "
            f"{len(iterated)} iterated; "
            f"avg score among iterated="
            f"{(sum((v.get('final_score') or 0) for v in iterated) / max(1, len(iterated))):.1f}/10"
        )

        # Headline app score.
        app_score = self.compute_app_score(views, self._acceptable_score)
        result["app_score"] = app_score
        not_reviewable = app_score.get("not_reviewable_routes") or []
        if app_score.get("mean") is not None:
            failing_summary = (
                f" — laggards: {', '.join(app_score['failing'][:3])}"
                if app_score["failing"] else ""
            )
            _log(
                self._on_status,
                f"ProUXDesigner: APP SCORE {app_score['mean']:.1f}/10 — "
                f"{app_score['passing']}/{app_score['covered']} passing "
                f"(min={app_score['min']} at "
                f"{app_score['min_route']}){failing_summary}"
            )
        if not_reviewable:
            # Surface separately — these aren't passing or failing,
            # they're "couldn't see the right page". Operator needs
            # to know they exist so the underlying redirect/auth
            # issue can be addressed at the engineering layer.
            _log(
                self._on_status,
                f"ProUXDesigner: {len(not_reviewable)} route(s) "
                f"not_reviewable (capture mismatch — not in APP SCORE): "
                f"{', '.join(not_reviewable[:5])}"
            )
            for v in views:
                if v.get("not_reviewable"):
                    _log(
                        self._on_status,
                        f"  not_reviewable: {v.get('route')} — "
                        f"{v.get('capture_mismatch_reason', '')[:200]}"
                    )

        # Append the run summary to the per-project log so the next
        # invocation can show the trend.
        try:
            capture_mismatch_count = sum(
                1 for v in views
                for it in (v.get("iterations") or [])
                if not it.get("captured_correctly", True)
            )
            plan_cache_hit = bool(
                self._timings.get("code_review_cached") is not None
                and self._timings.get("code_review") is None
            )
            summary_row = run_log.RunSummary(
                service=service.name,
                total_s=total,
                phase_timings=dict(self._timings),
                plan_cache_hit=plan_cache_hit,
                route_count=len(views),
                cached_count=cached_count,
                iterated_count=len(iterated),
                capture_mismatch_count=capture_mismatch_count,
                avg_score=avg_score,
                final_score_by_route={
                    v.get("route", "?"): v.get("final_score")
                    for v in views
                },
                stopped_reasons=[
                    v.get("stopped_reason") or "" for v in views
                ],
            )
            run_log.append_summary(ws_root_rl, summary_row)
        except Exception as e:
            _log(
                self._on_status,
                f"ProUXDesigner: run_log append failed "
                f"({type(e).__name__}: {e}) — non-fatal"
            )
        return result

    # ── Aggregate score across views ──────────────────────────────────

    @staticmethod
    def compute_app_score(views: List[Dict], acceptable_score: int = 7) -> Dict:
        """Roll the per-view scores up to an app-level metric.

        Excludes views marked ``not_reviewable`` (Ticket 2 — capture
        mismatches like ``/admin`` → ``/admin/users``). Those routes
        are surfaced via ``not_reviewable_routes`` so the operator
        can see them without them polluting the score.

        Returns a dict with:
          mean: float | None — average of all final_score values
          min:  int   | None — lowest final_score across views
          min_route: str | None — route owning the min (the bottleneck
                                  to fix next)
          passing: int — count of views with final_score >= acceptable
          failing: List[str] — routes that didn't meet the bar (sorted
                               by score ascending — laggards first)
          covered: int — number of views with a non-None final_score
          not_reviewable_routes: List[str] — routes skipped due to
                                             capture mismatch
          total:   int — total number of views (incl. not_reviewable)
        """
        scores = []
        scored_views = []
        not_reviewable_routes = []
        for v in views:
            if v.get("not_reviewable"):
                not_reviewable_routes.append(v.get("route", "?"))
                continue
            s = v.get("final_score")
            if s is None:
                continue
            scores.append(s)
            scored_views.append((v.get("route", "?"), s))
        if not scores:
            return {
                "mean": None, "min": None, "min_route": None,
                "passing": 0, "failing": [], "covered": 0,
                "not_reviewable_routes": not_reviewable_routes,
                "total": len(views),
            }
        mean = sum(scores) / len(scores)
        min_route, min_score = min(scored_views, key=lambda x: x[1])
        passing = sum(1 for _, s in scored_views if s >= acceptable_score)
        failing = sorted(
            (r for r, s in scored_views if s < acceptable_score),
            key=lambda r: dict(scored_views)[r],
        )
        return {
            "mean": round(mean, 2),
            "min": min_score,
            "min_route": min_route,
            "passing": passing,
            "failing": failing,
            "covered": len(scores),
            "not_reviewable_routes": not_reviewable_routes,
            "total": len(views),
        }

    # ── Logging helpers ───────────────────────────────────────────────

    def _log_debug(self, msg: str) -> None:
        if self._debug:
            _log(self._on_status, f"[DEBUG] {msg}")

    def _record_timing(self, key: str, elapsed: float) -> None:
        """Accumulate elapsed seconds under ``key``. Multiple calls
        with the same key sum (e.g. per-iteration phases)."""
        self._timings[key] = self._timings.get(key, 0.0) + elapsed

    def _format_timings(self) -> str:
        """Human-readable per-phase summary, longest first."""
        if not self._timings:
            return "(no timings recorded)"
        items = sorted(self._timings.items(), key=lambda x: -x[1])
        return ", ".join(f"{k}={v:.1f}s" for k, v in items)

    def _budget_for_route(self, cached_record) -> int:
        """Decide how many screenshot→eval→fix iterations to grant
        this route. Defaults to ``max_home_iterations``. Routes that
        hit the cap on a prior run AND scored below threshold get
        +2 extra iterations on the current run.

        ``cached_record`` is the optional ``ReviewRecord`` from the
        store (``None`` when there's no history yet).
        """
        default = self._max_home_iterations
        if cached_record is None:
            return default
        prior_iters = cached_record.iterations_to_acceptable or 0
        prior_score = cached_record.last_score
        if (
            prior_iters >= default
            and prior_score is not None
            and prior_score < self._acceptable_score
        ):
            return default + 2
        return default

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
        _t0 = time.time()
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
        self._record_timing("capture", time.time() - _t0)
        self._log_debug(
            f"{view_label} iter{iteration} capture: "
            f"{time.time() - _t0:.1f}s, {len(screenshots)} screenshots"
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

        # Sentinel check: did Playwright actually land on the route
        # we asked for? Each screenshot has a sibling .meta.json with
        # the final URL after redirects. Mismatch (e.g. dashboard
        # captured login because we weren't authed; /recipes/:id
        # captured /recipes/new because the wrong link was clicked)
        # is the dominant root cause of low-score iterations spinning
        # on the wrong page.
        # ``sibling_routes`` is set via ProUXDesigner._sibling_routes
        # in the per-view dispatch — we tee it here for the collision
        # check.
        capture_ok, capture_reason = self._verify_capture(
            route=route, screenshots=screenshots,
            sibling_routes=getattr(self, "_sibling_routes", None),
        )
        # Short-circuit on capture mismatch (Ticket 2). When Playwright
        # landed on a different URL than we asked for (admin index
        # redirected to admin/users; recipes redirected to dashboard;
        # protected route bounced to login; dynamic sibling collision),
        # the eval would score the WRONG page — inflating APP SCORE and
        # caching a misleading record. Mark the view ``not_reviewable``,
        # skip eval+fix, save tokens, and let the harness diagnose the
        # redirect rather than chase phantom fixes.
        if not capture_ok:
            _log(
                self._on_status,
                f"ProUXDesigner: {view_label} iter {iteration} — "
                f"capture mismatch: {capture_reason}; skipping eval+fix "
                f"(view marked not_reviewable)"
            )
            return {
                "iteration": iteration,
                "screenshots": [str(s["path"]) for s in screenshots],
                "evaluation": None,
                "fix_result": None,
                "initial_score": None,
                "final_score": None,
                "captured_correctly": False,
                "capture_mismatch_reason": capture_reason,
                "not_reviewable": True,
                "stop": True,
                "stop_reason": f"capture mismatch: {capture_reason}",
            }

        _log(
            self._on_status,
            f"ProUXDesigner: {view_label} iter {iteration} — evaluating..."
        )
        _t0 = time.time()
        evaluation = self._evaluate_view(
            screenshots=screenshots,
            route=route,
            view_meta=view_meta,
            view_label=view_label,
            iteration=iteration,
            design_plan=design_plan,
            service=service,
        )
        self._record_timing("eval", time.time() - _t0)
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
            "captured_correctly": True,
            "capture_mismatch_reason": None,
            "not_reviewable": False,
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
        _t0 = time.time()
        fix_result = self._apply_view_fixes(
            issues=issues,
            view_meta=view_meta,
            view_label=view_label,
            design_plan=design_plan,
            service=service, workspace=workspace,
        )
        self._record_timing("fix", time.time() - _t0)
        out["fix_result"] = fix_result
        _log(
            self._on_status,
            f"ProUXDesigner: {view_label} iter {iteration} — applied "
            f"{len(fix_result.get('files_written', []))} file change(s)"
        )
        return out

    # ── Helpers ────────────────────────────────────────────────────────

    def _verify_capture(
        self,
        route: str,
        screenshots: List[Dict],
        sibling_routes: Optional[List[str]] = None,
    ) -> tuple:
        """Read each screenshot's sibling ``<name>.meta.json`` and
        verify the captured URL matches the requested route. Returns
        ``(ok, reason)``.

        Two failure modes covered:
          1. URL pattern doesn't match at all (auth-protected route
             captured as ``/login``, etc.).
          2. URL pattern matches a dynamic route but the captured
             pathname is a more-specific sibling — e.g. requested
             ``/recipes/:id`` but captured ``/recipes/new`` because
             Strategy A clicked the New Recipe CTA. We catch this by
             checking ``sibling_routes`` for any literal match.

        Missing meta file → pass through (older captures predating
        the meta-emission contract).
        """
        if not screenshots:
            return (False, "no screenshots")
        expected_re = self._route_to_regex(route)
        siblings = [
            s for s in (sibling_routes or [])
            if s != route and ":" not in s
        ]
        for s in screenshots:
            sp = Path(s["path"])
            meta_path = sp.with_suffix(".meta.json")
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                continue
            actual = (meta.get("final_pathname") or "").rstrip("/") or "/"
            if not expected_re.match(actual):
                return (
                    False,
                    f"expected {route!r}, captured {actual!r} "
                    f"(file={sp.name})"
                )
            # Dynamic-route collision: if the route is dynamic and
            # the actual pathname is one of the known LITERAL sibling
            # routes, the dynamic-param resolver clearly grabbed the
            # wrong target.
            if ":" in route and actual in siblings:
                return (
                    False,
                    f"requested dynamic {route!r}, captured the literal "
                    f"sibling {actual!r} (collision; check route order or "
                    f"link selector)"
                )
        return (True, "")

    @staticmethod
    def _route_to_regex(route: str):
        """Turn ``/recipes/:id/edit`` into a regex that matches
        ``/recipes/<anything-but-slash>/edit``. Trailing slash and
        query string are ignored. Colon is not a regex metachar so
        we substitute on the plain ``:name`` form before any escape."""
        # Substitute :params with a placeholder, escape, then swap
        # the placeholder for the real regex segment. This avoids
        # the asymmetry between re.escape's behavior on different
        # Python versions.
        SENTINEL = "\x00PARAM\x00"
        normalized = re.sub(r":[A-Za-z_]\w*", SENTINEL, route)
        escaped = re.escape(normalized)
        with_params = escaped.replace(re.escape(SENTINEL), r"[^/]+")
        return re.compile(f"^{with_params}/?$")

    def _translate_to_concrete(self, routes: List[str]) -> List[str]:
        """Substitute dynamic templates with the resolver's concrete
        URLs. Returns the input list unchanged when no resolutions
        are known (e.g. resolver fell back / disabled). The mapping
        is ``_resolved_url_by_template`` set in review_frontend."""
        mapping = getattr(self, "_resolved_url_by_template", {}) or {}
        if not mapping:
            return list(routes)
        return [mapping.get(r, r) for r in routes]

    def _rekey_to_templates(
        self, buckets: Dict[str, List[Dict]],
    ) -> Dict[str, List[Dict]]:
        """Reverse the concrete-URL substitution applied at capture
        time. Playwright writes ``requested_route`` = whatever we
        passed in (e.g. ``/recipes/abc-123``); the per-view loop and
        review cache key by template (``/recipes/:id``). Walk every
        bucket and re-key the dynamic ones back to their template.
        Static routes pass through unchanged."""
        mapping = getattr(self, "_resolved_url_by_template", {}) or {}
        if not mapping or not buckets:
            return buckets
        # Build inverse: concrete_url → template.
        inverse = {v: k for k, v in mapping.items()}
        out: Dict[str, List[Dict]] = {}
        for key, shots in buckets.items():
            new_key = inverse.get(key, key)
            out.setdefault(new_key, []).extend(shots)
        return out

    def _find_openapi(self, workspace_root: Path) -> Optional[Path]:
        """Best-effort OpenAPI discovery. The integration phase writes
        ``<project>/contracts/<service>.openapi.json``. From a frontend
        workspace, that's ``workspace.parent/contracts/``. Returns the
        first match, or None."""
        project_root = workspace_root.parent
        contracts_dir = project_root / "contracts"
        if not contracts_dir.is_dir():
            return None
        candidates = sorted(contracts_dir.glob("*.openapi.json"))
        return candidates[0] if candidates else None

    def _bucket_shots_by_route(
        self, shots: List[Dict],
    ) -> Dict[str, List[Dict]]:
        """Group a flat shot list by ``requested_route`` from each
        shot's sibling ``.meta.json``. Shots without a meta file
        (older Playwright generations) are skipped — we can't bucket
        them without the source-of-truth route field."""
        out: Dict[str, List[Dict]] = {}
        for s in shots:
            sp = Path(s["path"])
            meta_path = sp.with_suffix(".meta.json")
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                continue
            route = meta.get("requested_route")
            if not route:
                continue
            out.setdefault(route, []).append(s)
        return out

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
        the multi-route loop.

        Optimization: on ``iteration == 1`` we prefer pre-captured
        shots from the full-multi-route Playwright run (populated in
        ``review_frontend`` before the loop starts). On ``iteration
        >= 2`` we always re-capture because the workspace has been
        edited since iter 1 — the cached shot would be stale.
        """
        precaptured = getattr(self, "_precaptured_by_route", {}) or {}
        if iteration == 1 and precaptured.get(route):
            shots = list(precaptured[route])
            self._log_debug(
                f"{view_label} iter1: using {len(shots)} pre-captured shot(s)"
            )
        else:
            concrete = self._translate_to_concrete([route])
            all_shots = self._take_screenshots(
                service=service,
                workspace=workspace,
                compose_path=compose_path,
                problem_statement=problem_statement,
                milestone_scope=milestone_scope,
                routes=concrete,
                auth_contract=auth_contract,
                backend_url=backend_url,
            )
            # The screenshot prompt asks for one test per route, but
            # Claude routinely auto-discovers OTHER routes in the
            # workspace and emits tests for them too. When we fall
            # through to per-route capture, ``all_shots`` is the
            # full grab-bag (10+ PNGs from a 1-route request). Bucket
            # by ``requested_route`` from each shot's meta, re-map
            # concrete URLs back to templates, and keep only the ones
            # for the route we actually asked about.
            buckets = self._rekey_to_templates(
                self._bucket_shots_by_route(all_shots),
            )
            shots = buckets.get(route, [])
            self._log_debug(
                f"{view_label} iter{iteration}: per-route capture "
                f"returned {len(all_shots)} shots; "
                f"{len(shots)} match route={route}"
            )
            if not shots and all_shots:
                # Fallback: model didn't emit a test with the right
                # requested_route metadata. Take the shots with no
                # meta (older capture path) or accept all — better
                # than nothing.
                shots = [
                    s for s in all_shots
                    if not Path(s["path"]).with_suffix(".meta.json").exists()
                ] or all_shots
                self._log_debug(
                    f"{view_label} iter{iteration}: no route-matched "
                    f"shots; using {len(shots)} unmatched as fallback"
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
            # Also copy the sibling meta.json so Stage B's
            # _verify_capture can read it from the iter dir. Without
            # this, the verifier silently passes through (treating
            # missing meta as "older capture") and dynamic-route
            # collisions / unintended redirects slip past.
            meta_src = src.with_suffix(".meta.json")
            if meta_src.exists():
                try:
                    shutil.copy2(meta_src, dest.with_suffix(".meta.json"))
                except Exception:
                    pass
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
        max_attempts: int = 6,
        backoff_seconds: tuple = (0.0, 10.0, 30.0, 60.0, 90.0, 110.0),
    ) -> Dict:
        """Verify Tailwind CSS is being served by the dev container.

        Retries with backoff to absorb dev-server cold-build races:

          - Vite + PostCSS warm-up after container restart (5-10s)
          - Vite first-build on a cold cache after a global_design
            step writes 20+ files (30-60s)
          - Angular cold build (the whole bundle compile, 3-5 min)

        Schedule: 0 + 10 + 30 + 60 + 90 + 110 = 300s total worst case.
        Warm builds return immediately at attempt 1. crm_v1 M3 case
        (Vite + 23 new files, ~30-60s) settles around attempt 3-4.
        Angular cold builds settle around attempt 5-6.

        Returns ``{ok, detail, css_urls, attempts}`` — same shape as
        the original single-shot probe plus ``attempts`` so the
        caller can see how long it took to settle.
        """
        last_result = None
        for attempt in range(max_attempts):
            wait = backoff_seconds[attempt] if attempt < len(backoff_seconds) else backoff_seconds[-1]
            if wait > 0:
                time.sleep(wait)
            result = self._probe_tailwind_once(service, compose_path)
            result["attempts"] = attempt + 1
            last_result = result
            if result.get("ok"):
                return result
        return last_result or {
            "ok": False, "detail": "no probe attempts ran",
            "css_urls": [], "attempts": 0,
        }

    def _probe_tailwind_once(
        self,
        service: ServiceDefinition,
        compose_path: str,
    ) -> Dict:
        """Single-shot tailwind-serving probe. Fetches the rendered
        HTML, follows linked stylesheets + inline ``<style>`` blocks,
        greps for ``--tw-*`` markers. See ``_verify_tailwind_serving``
        for the retry-with-backoff wrapper."""
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
