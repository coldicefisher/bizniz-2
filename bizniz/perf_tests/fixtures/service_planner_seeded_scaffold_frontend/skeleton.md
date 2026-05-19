# React Skeleton — Directory Contract

Read this **before** generating code. The skeleton's `src/App.tsx`,
auth, layout, and toast machinery are already wired up. Your code
goes into the extension points below. **Never edit a file the
skeleton ships** — create new files instead.

## The four extension points

| You add | Skeleton ships | Mounted by |
|---|---|---|
| `src/routes/<feature>.tsx` | `docs.tsx` | yes — `App.tsx` auto-discovers via `import.meta.glob` |
| `src/pages/<feature>.tsx` | `HomePage.tsx`, `LoginPage.tsx`, `DocsPage.tsx` | imported by your route module |
| `src/api/<feature>.ts` | `auth.ts`, `client.ts`, `docs.ts` | imported where used |
| `src/components/<feature>.tsx` | `layout/AppLayout`, `layout/Topbar`, `common/Toast`, `docs/*` | imported where used |

## UI primitives (ship-with-skeleton, extend — don't replace)

Every project ships with a starter set of design-system primitives
under `src/components/ui/`, each paired with a `.stories.tsx` so
Storybook (and the bizniz UX gate) has them on day one:

| File | Component | Canonical states |
|---|---|---|
| `src/components/ui/Button.tsx` | `Button` | idle, loading, disabled, + variants (primary/secondary/ghost/danger), sizes (sm/md/lg) |
| `src/components/ui/FormInput.tsx` | `FormInput` | default, filled, invalid, disabled |
| `src/components/ui/Modal.tsx` | `Modal` | closed (returns null), open |
| `src/components/ui/Alert.tsx` | `Alert` | info, success, warning, error, dismissable |

Plus `src/components/common/Toast.tsx` (already shipped).

**The contract:**

- Each primitive's root element exposes `data-bizniz-primitive="<name>"`
  + `data-state="<state>"` for the UX pipeline's interaction-test phase
  to target.
- Every canonical state has a matching story in the sibling
  `.stories.tsx`. The Engineer phase MAY add stories for new variants
  but never removes the canonical-state set.
- You may add NEW primitives under `src/components/ui/` (with stories).
  Do NOT edit the shipped primitives' interfaces — extend by composition.

## Docs surface (always present — never edit)

Every generated app ships with a working `/docs` route that
renders the markdown HumanDocsGenerator writes to
`<project>/docs/`. Files:

- `src/pages/DocsPage.tsx` — layout (sidebar + article pane)
- `src/components/docs/DocsArticle.tsx` — react-markdown renderer with anchor-link handling
- `src/components/docs/DocsNavTree.tsx` — recursive sidebar nav
- `src/components/docs/DocsSearch.tsx` — debounced search dropdown
- `src/api/docs.ts` — client for `/api/v1/docs/*`
- `src/types/docs.ts` — TypeScript types mirroring the backend DTOs
- `src/routes/docs.tsx` — auto-mounted route entries

The backend exposes `/api/v1/docs/{index,article/{slug:path},search}`
(see fastapi skeleton's `app/api/routes/docs.py`). The viewer
authenticates via the same `useAuthStore` token used elsewhere.

**Do not edit these files.** If you need a docs-related capability
(e.g. "admin-only docs section"), add a NEW route/page that imports
from `@/api/docs` and reuses the existing components.

## Adding a new feature (example: services + appointments)

1. **Page component** — create `src/pages/ServicesPage.tsx`:
   ```tsx
   import { useEffect, useState } from "react";
   import { listServices } from "@/api/services";

   export default function ServicesPage() {
     const [services, setServices] = useState<Service[]>([]);
     useEffect(() => { listServices().then(setServices); }, []);
     return <ul>{services.map(s => <li key={s.id}>{s.name}</li>)}</ul>;
   }
   ```

2. **API client** — create `src/api/services.ts`:
   ```ts
   import { request } from "@/api/client";
   export async function listServices(): Promise<Service[]> {
     return request<Service[]>("/api/v1/services");
   }
   ```

3. **Route registration** — create `src/routes/services.tsx`:
   ```tsx
   import ServicesPage from "@/pages/ServicesPage";
   export default [
     { path: "/services", element: <ServicesPage /> },
   ];
   ```

   That's it. `App.tsx` auto-discovers `src/routes/*.tsx` and mounts
   every entry. No edit to `App.tsx` required.

## Pages you may replace

`src/pages/HomePage.tsx` and `src/pages/LoginPage.tsx` ship as
placeholders. You may rewrite their content (they're meant to be
replaced with the real domain home / real login form) — but DO NOT
delete them, and the file paths must stay. They're already wired
into `App.tsx`'s top-level `<Routes>`.

## Storybook — primitive contract (CRITICAL for UX pipeline)

**Every component in `src/components/ui/` MUST ship a sibling
`<Component>.stories.tsx` file.** The bizniz UX-review pipeline runs
interaction tests against these stories; a primitive without a story
is invisible to that phase.

Each story file:
- Exports a `Meta` default export with the component title +
  component reference.
- Exports one named `Story` per canonical state. For interactive
  primitives include at minimum: `Default`, `Loading`, `Disabled`,
  `Error` (if applicable).
- May provide a `play` function to exercise interactions
  (`@storybook/test`'s `userEvent` + `expect`).

Example: see `src/components/common/Toast.stories.tsx` — ships with
the skeleton as the canonical pattern.

Run Storybook locally with `npm run storybook` (default port 6006).
The pipeline runs `npm run build-storybook` to produce a static
catalog the UX phase iterates against.

Tests files inside `*.stories.tsx` are excluded from Jest via the
`testPathIgnorePatterns` entry in `package.json` — story files are
NOT unit tests, they're catalog entries.

## Test file placement (CRITICAL)

**Test files (`*.test.ts`, `*.test.tsx`, `*.spec.ts`, `*.spec.tsx`)
go in `src/__tests__/<feature>.test.tsx`. NEVER co-locate test
files in `src/routes/`, `src/pages/`, or `src/api/`.**

Why: Vite's runtime bundler eagerly imports route modules via
`import.meta.glob`. A test file in `src/routes/` would be loaded
in the browser, where Jest globals like `describe()` don't exist —
the entire frontend crashes on first paint with
"ReferenceError: describe is not defined".

The skeleton's `App.tsx` glob already excludes `*.test.tsx` and
`*.spec.tsx` from `src/routes/`, but the same risk exists for any
auto-discovery you might add. **Default to `src/__tests__/`.**

Correct:
- `src/routes/grooming.tsx` ← route registration
- `src/__tests__/routes/grooming.test.tsx` ← its tests
- `src/pages/ServicesPage.tsx` ← page component
- `src/__tests__/pages/ServicesPage.test.tsx` ← its tests

Incorrect:
- `src/routes/grooming.test.tsx` ← would crash browser
- `src/pages/ServicesPage.test.tsx` ← clutters runtime tree

## What you may NOT do

- ❌ Edit `src/App.tsx`. Auto-discovery handles new routes.
- ❌ Edit `src/main.tsx`. The Vite entry point is wired.
- ❌ Edit `src/api/client.ts`, `src/api/auth.ts`, `src/stores/authStore.ts`,
  `src/components/layout/*`, `src/components/common/Toast.tsx`.
  Downstream code imports specific symbols from these. Add new
  files; never edit shipped ones.
- ❌ Create files outside `src/`. The Vite root expects `src/`.
- ❌ Create a parallel package directory like `app/` or
  `<service_name>/` next to `src/`. Vite won't see it.
- ❌ Rewrite a shipped file to "simplify" or "clean it up." Add
  parallel files in the extension points instead.
- ❌ Co-locate test files in `src/routes/`, `src/pages/`, or
  `src/api/`. Tests go in `src/__tests__/`. Co-located tests in
  `src/routes/` will crash the browser at runtime.
- ❌ Create `__init__.py` or any Python file. This is a TypeScript
  project.

## Design system: Tailwind CSS v4

This skeleton uses **Tailwind CSS v4** for all styling. Use Tailwind
utility classes directly in JSX — do NOT write custom CSS files or
use inline `style` props.

```tsx
// Good
<button className="bg-primary text-white px-4 py-2 rounded-lg hover:bg-primary-dark">
  Save
</button>

// Bad — do not write custom CSS
<button style={{ background: '#2563eb' }}>Save</button>
```

The theme is configured in `src/styles/global.css` with custom
colors (`primary`, `danger`, `success`, `warning`). Extend the
theme there if the design needs new colors.

## The contract in one sentence

Drop new pages in `src/pages/<feature>.tsx`, register them via
`src/routes/<feature>.tsx`, add API helpers in
`src/api/<feature>.ts`. Never edit the files the skeleton ships
(except `HomePage.tsx`/`LoginPage.tsx` content, which are explicit
replacement points).
