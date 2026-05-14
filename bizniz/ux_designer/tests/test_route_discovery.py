"""Tests for deterministic route discovery (Tier 1)."""
from pathlib import Path
from textwrap import dedent

import pytest

from bizniz.ux_designer.route_discovery import (
    RouteSpec,
    discover_angular_routes,
    discover_react_routes,
    discover_routes,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip())


class TestReactRoutes:
    def test_bizniz_skeleton_one_file_per_route(self, tmp_path):
        # The recipe_box convention: each src/routes/<name>.tsx exports
        # default { path: "/foo", element: <Foo /> }.
        _write(tmp_path / "src/routes/home.tsx", '''
            const routeEntry = { path: "/", element: <HomePage /> };
            export default routeEntry;
        ''')
        _write(tmp_path / "src/routes/recipe-detail.tsx", '''
            const routeEntry = { path: "/recipes/:id", element: <RecipeDetail /> };
            export default routeEntry;
        ''')
        _write(tmp_path / "src/routes/admin.tsx", '''
            const routeEntry = { path: "/admin", element: <Admin /> };
            export default routeEntry;
        ''')
        routes = discover_react_routes(tmp_path)
        paths = sorted(r.path for r in routes)
        assert paths == ["/", "/admin", "/recipes/:id"]

    def test_dynamic_routes_marked(self, tmp_path):
        _write(tmp_path / "src/routes/recipe.tsx", '''
            const routeEntry = { path: "/recipes/:id/edit", element: <X /> };
            export default routeEntry;
        ''')
        routes = discover_react_routes(tmp_path)
        assert len(routes) == 1
        assert routes[0].is_dynamic is True
        assert routes[0].params == ["id"]

    def test_jsx_route_pattern(self, tmp_path):
        _write(tmp_path / "src/App.tsx", '''
            export default function App() {
              return (
                <Routes>
                  <Route path="/" element={<Home />} />
                  <Route path="/login" element={<Login />} />
                  <Route path="/users/:userId" element={<User />} />
                </Routes>
              );
            }
        ''')
        routes = discover_react_routes(tmp_path)
        paths = sorted(r.path for r in routes)
        assert paths == ["/", "/login", "/users/:userId"]
        user_route = next(r for r in routes if r.path == "/users/:userId")
        assert user_route.params == ["userId"]

    def test_test_files_skipped(self, tmp_path):
        _write(tmp_path / "src/routes/home.tsx", '''
            const routeEntry = { path: "/", element: <H /> };
            export default routeEntry;
        ''')
        # These should NOT contribute fake routes.
        _write(tmp_path / "src/routes/home.test.tsx",
               'const x = { path: "/fake-from-test" };')
        _write(tmp_path / "src/routes/home.spec.tsx",
               'const x = { path: "/fake-from-spec" };')
        routes = discover_react_routes(tmp_path)
        paths = [r.path for r in routes]
        assert paths == ["/"]
        assert all("fake-from" not in p for p in paths)

    def test_empty_workspace_returns_empty(self, tmp_path):
        assert discover_react_routes(tmp_path) == []

    def test_dedupes_when_same_path_in_multiple_files(self, tmp_path):
        _write(tmp_path / "src/routes/a.tsx",
               'const r = { path: "/" };')
        _write(tmp_path / "src/App.tsx",
               '<Route path="/" element={<X />} />')
        routes = discover_react_routes(tmp_path)
        assert len([r for r in routes if r.path == "/"]) == 1


class TestAngularRoutes:
    def test_routing_module(self, tmp_path):
        _write(tmp_path / "src/app/app-routing.module.ts", '''
            const routes: Routes = [
              { path: 'home', component: HomeComponent },
              { path: 'recipes/:id', component: RecipeComponent },
              { path: 'admin', component: AdminComponent },
            ];
            @NgModule({ imports: [RouterModule.forRoot(routes)] })
            export class AppRoutingModule {}
        ''')
        routes = discover_angular_routes(tmp_path)
        paths = sorted(r.path for r in routes)
        assert paths == ["/admin", "/home", "/recipes/:id"]

    def test_dynamic_param_extracted(self, tmp_path):
        _write(tmp_path / "src/app/app.routes.ts", '''
            export const APP_ROUTES: Routes = [
              { path: 'users/:userId/edit', component: UserEdit },
            ];
        ''')
        routes = discover_angular_routes(tmp_path)
        assert len(routes) == 1
        assert routes[0].path == "/users/:userId/edit"
        assert routes[0].params == ["userId"]

    def test_nested_routing_modules(self, tmp_path):
        _write(tmp_path / "src/app/admin/admin-routing.module.ts", '''
            const routes: Routes = [
              { path: 'admin/users', component: AdminUsers },
            ];
        ''')
        routes = discover_angular_routes(tmp_path)
        assert any(r.path == "/admin/users" for r in routes)


class TestDispatcher:
    def test_react_framework_uses_react_parser(self, tmp_path):
        _write(tmp_path / "src/routes/home.tsx",
               'const r = { path: "/" };')
        routes = discover_routes(tmp_path, framework="react")
        assert any(r.path == "/" for r in routes)

    def test_angular_framework_uses_angular_parser(self, tmp_path):
        _write(tmp_path / "src/app/app.routes.ts",
               "const routes: Routes = [{ path: 'home', component: H }];")
        routes = discover_routes(tmp_path, framework="angular")
        assert any(r.path == "/home" for r in routes)

    def test_unknown_framework_falls_through(self, tmp_path):
        # React-style files: dispatcher should still find them.
        _write(tmp_path / "src/routes/home.tsx",
               'const r = { path: "/" };')
        routes = discover_routes(tmp_path, framework="mystery")
        assert any(r.path == "/" for r in routes)

    def test_empty_returns_empty(self, tmp_path):
        assert discover_routes(tmp_path, framework="react") == []


class TestAgentFallback:
    def _fake_proc(self, result_text, returncode=0):
        import json
        from unittest.mock import MagicMock
        p = MagicMock()
        p.stdout = json.dumps({
            "type": "result", "is_error": False,
            "result": result_text, "session_id": "sid",
        })
        p.stderr = ""
        p.returncode = returncode
        return p

    def test_agent_parses_well_formed_response(self, tmp_path):
        import json as _json
        from unittest.mock import patch
        from bizniz.ux_designer.route_discovery import agent_discover_routes
        agent_json = _json.dumps({
            "routes": [
                {"path": "/", "params": [], "is_dynamic": False,
                 "source_file": "src/App.tsx"},
                {"path": "/users/:id", "params": ["id"], "is_dynamic": True,
                 "source_file": "src/App.tsx"},
            ],
            "notes": "",
        })
        with patch("bizniz.ux_designer.route_discovery.shutil.which",
                   return_value="/usr/bin/claude"), \
             patch("bizniz.ux_designer.route_discovery.subprocess.run",
                   return_value=self._fake_proc(agent_json)):
            out = agent_discover_routes(tmp_path, framework="unknown")
        paths = sorted(r.path for r in out)
        assert paths == ["/", "/users/:id"]
        # is_dynamic computed from path, not trusted from agent payload.
        users = next(r for r in out if r.path == "/users/:id")
        assert users.is_dynamic is True
        assert users.params == ["id"]

    def test_agent_returns_empty_when_binary_missing(self, tmp_path):
        from unittest.mock import patch
        from bizniz.ux_designer.route_discovery import agent_discover_routes
        with patch("bizniz.ux_designer.route_discovery.shutil.which",
                   return_value=None):
            out = agent_discover_routes(tmp_path)
        assert out == []

    def test_combined_uses_tier_1_when_found(self, tmp_path):
        from unittest.mock import patch
        from bizniz.ux_designer.route_discovery import (
            discover_routes_with_fallback,
        )
        _write(tmp_path / "src/routes/home.tsx",
               'const r = { path: "/" };')
        with patch("bizniz.ux_designer.route_discovery.subprocess.run") as m:
            out = discover_routes_with_fallback(
                tmp_path, framework="react",
            )
        assert any(r.path == "/" for r in out)
        # Tier 2 must NOT have been called when Tier 1 returned routes.
        assert m.call_count == 0

    def test_combined_falls_through_to_agent(self, tmp_path):
        import json as _json
        from unittest.mock import patch
        from bizniz.ux_designer.route_discovery import (
            discover_routes_with_fallback,
        )
        # No route files on disk — Tier 1 returns [], should fall through.
        agent_json = _json.dumps({"routes": [{"path": "/", "params": []}]})
        with patch("bizniz.ux_designer.route_discovery.shutil.which",
                   return_value="/usr/bin/claude"), \
             patch("bizniz.ux_designer.route_discovery.subprocess.run",
                   return_value=self._fake_proc(agent_json)) as m:
            out = discover_routes_with_fallback(
                tmp_path, framework="react",
            )
        assert m.call_count == 1
        assert any(r.path == "/" for r in out)


class TestPluggableAgent:
    def test_agent_fn_is_called_instead_of_default(self, tmp_path):
        from bizniz.ux_designer.route_discovery import (
            RouteSpec, discover_routes_with_fallback,
        )
        called_with = []

        def fake_agent(ws, fw):
            called_with.append((ws, fw))
            return [RouteSpec(path="/from-fake-agent")]

        out = discover_routes_with_fallback(
            tmp_path, framework="weird",
            agent_fn=fake_agent,
        )
        # Tier 1 returned nothing (empty workspace) → fake_agent fired.
        assert len(called_with) == 1
        assert called_with[0] == (tmp_path, "weird")
        assert [r.path for r in out] == ["/from-fake-agent"]

    def test_agent_fn_skipped_when_tier_1_finds_routes(self, tmp_path):
        from bizniz.ux_designer.route_discovery import (
            discover_routes_with_fallback,
        )
        _write(tmp_path / "src/routes/home.tsx",
               'const r = { path: "/" };')
        called = []

        def fake_agent(ws, fw):
            called.append(1)
            return []

        out = discover_routes_with_fallback(
            tmp_path, framework="react",
            agent_fn=fake_agent,
        )
        assert any(r.path == "/" for r in out)
        assert called == []  # Tier 1 was enough


class TestAuthDetection:
    def test_react_require_auth_wrapper_marks_protected(self, tmp_path):
        _write(tmp_path / "src/routes/dashboard.tsx", '''
            const routeEntry = {
              path: "/dashboard",
              element: <RequireAuth><Dashboard /></RequireAuth>,
            };
            export default routeEntry;
        ''')
        routes = discover_react_routes(tmp_path)
        assert len(routes) == 1
        assert routes[0].requires_auth is True
        assert "RequireAuth" in routes[0].auth_signals

    def test_react_admin_guard(self, tmp_path):
        _write(tmp_path / "src/routes/admin.tsx", '''
            const routeEntry = {
              path: "/admin",
              element: <AdminRouteGuard><AdminPanel /></AdminRouteGuard>,
            };
            export default routeEntry;
        ''')
        routes = discover_react_routes(tmp_path)
        assert routes[0].requires_auth is True
        assert "AdminRouteGuard" in routes[0].auth_signals

    def test_react_login_route_marked_public(self, tmp_path):
        _write(tmp_path / "src/routes/login.tsx", '''
            const routeEntry = { path: "/login", element: <Login /> };
            export default routeEntry;
        ''')
        routes = discover_react_routes(tmp_path)
        assert routes[0].requires_auth is False

    def test_react_unwrapped_route_unknown(self, tmp_path):
        # Generic non-guarded, non-public-named route → leave as None.
        _write(tmp_path / "src/routes/about.tsx", '''
            const routeEntry = { path: "/about", element: <About /> };
            export default routeEntry;
        ''')
        routes = discover_react_routes(tmp_path)
        # /about is in the public-named set.
        assert routes[0].requires_auth is False

    def test_react_jsx_routes_wrapper_context(self, tmp_path):
        _write(tmp_path / "src/App.tsx", '''
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/login" element={<Login />} />
              <RequireAuth>
                <Route path="/dashboard" element={<Dashboard />} />
              </RequireAuth>
            </Routes>
        ''')
        routes = discover_react_routes(tmp_path)
        d = {r.path: r for r in routes}
        assert d["/dashboard"].requires_auth is True
        # /login is in the public-named set.
        assert d["/login"].requires_auth is False
        # / is also public.
        assert d["/"].requires_auth is False

    def test_angular_canactivate_marks_protected(self, tmp_path):
        _write(tmp_path / "src/app/app.routes.ts", '''
            const routes: Routes = [
              { path: 'dashboard', component: DashboardComponent,
                canActivate: [AuthGuard] },
              { path: 'login', component: LoginComponent },
            ];
        ''')
        routes = discover_angular_routes(tmp_path)
        d = {r.path: r for r in routes}
        assert d["/dashboard"].requires_auth is True
        assert "canActivate" in d["/dashboard"].auth_signals
        assert d["/login"].requires_auth is False


class TestTextClientAgent:
    def test_returns_empty_without_client(self, tmp_path):
        from bizniz.ux_designer.route_discovery import (
            text_client_agent_discover_routes,
        )
        out = text_client_agent_discover_routes(tmp_path, client=None)
        assert out == []

    def test_returns_empty_when_no_candidate_files(self, tmp_path):
        from unittest.mock import MagicMock
        from bizniz.ux_designer.route_discovery import (
            text_client_agent_discover_routes,
        )
        client = MagicMock()
        out = text_client_agent_discover_routes(tmp_path, client=client)
        assert out == []
        # No client call because there's nothing to send.
        client.get_text.assert_not_called()

    def test_calls_client_with_rendered_files_and_parses_response(self, tmp_path):
        import json
        from unittest.mock import MagicMock
        from bizniz.ux_designer.route_discovery import (
            text_client_agent_discover_routes,
        )
        _write(tmp_path / "src/App.tsx",
               '<Route path="/" element={<H />} />')
        client = MagicMock()
        client.get_text.return_value = (
            json.dumps({"routes": [
                {"path": "/", "source_file": "src/App.tsx"},
                {"path": "/profile/:id"},
            ]}),
            "session-id",
            [],
        )
        out = text_client_agent_discover_routes(
            tmp_path, framework="react", client=client,
        )
        paths = sorted(r.path for r in out)
        assert paths == ["/", "/profile/:id"]
        # is_dynamic derived from path, not trusted from agent.
        profile = next(r for r in out if r.path == "/profile/:id")
        assert profile.is_dynamic is True

    def test_client_exception_returns_empty(self, tmp_path):
        from unittest.mock import MagicMock
        from bizniz.ux_designer.route_discovery import (
            text_client_agent_discover_routes,
        )
        _write(tmp_path / "src/App.tsx",
               '<Route path="/" element={<H />} />')
        client = MagicMock()
        client.get_text.side_effect = RuntimeError("provider down")
        out = text_client_agent_discover_routes(
            tmp_path, framework="react", client=client,
        )
        assert out == []
