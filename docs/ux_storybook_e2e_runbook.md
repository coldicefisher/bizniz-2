# Storybook UX Loop — Live End-to-End Runbook

The per-story Storybook UX loop (roadmap item 2 done-when) is
fully scaffolded in code (Phases 1–6) and unit-validated via
mocks. Live end-to-end validation against a real Storybook server
+ real Playwright + real vision model is documented here.

This is the manual procedure to run when validating Storybook in
a real build (or when debugging the loop). Automated pytest
coverage lives at `bizniz/ux_designer/tests/test_storybook_*.py`.

## Prerequisites

- `node` + `npm` on PATH
- A frontend workspace with `package.json` containing a
  `"storybook"` script (the React skeleton ships this)
- `playwright` installed in the workspace (the sidecar image
  bundles it; for host-local runs, `npm install playwright` in
  the frontend dir)
- `claude` CLI on PATH (for vision eval + fix dispatch)
- A `.storybook/main.ts` pointing at the workspace's stories

## Phase-by-phase manual validation

### Phase 1 (discovery) — already validated

```bash
PYTHONPATH=. .venv/bin/python -c "
from pathlib import Path
from bizniz.ux_designer.storybook_discovery import discover_stories
catalog = discover_stories(Path.home() / 'bizniz-skeleton-react')
print(f'stories: {catalog.story_count}')
for s in catalog.stories:
    print(f'  {s.story_id}  file={s.component_file}')
"
```

Expected: 2 stories from `Toast.stories.tsx` with resolved
component files.

### Phase 2 (server + capture) — needs node

```bash
cd ~/bizniz-skeleton-react
npm install
npm run storybook -- --port 6006 --host 0.0.0.0 --ci --quiet --no-open &
# Wait for "Local: http://localhost:6006" to appear
```

Then from another terminal:

```bash
PYTHONPATH=. .venv/bin/python -c "
from pathlib import Path
import tempfile
from bizniz.ux_designer.storybook_capture import capture_stories
from bizniz.ux_designer.storybook_discovery import discover_stories

catalog = discover_stories(Path.home() / 'bizniz-skeleton-react')
with tempfile.TemporaryDirectory() as d:
    results = capture_stories(
        catalog=catalog,
        storybook_base_url='http://localhost:6006',
        output_dir=Path(d),
        on_status=print,
    )
    for r in results:
        print(f'{r.story_id}: success={r.success}, path={r.screenshot_path}')
"
```

Expected: PNG written to disk per story, all `success=True`.

### Phase 3 (evaluator) — needs claude CLI + a captured PNG

```bash
PYTHONPATH=. .venv/bin/python -c "
from pathlib import Path
from bizniz.ux_designer.storybook_eval import StoryEvaluator
from bizniz.ux_designer.storybook_discovery import discover_stories
from bizniz.ux_designer.storybook_capture import StoryCaptureResult

catalog = discover_stories(Path.home() / 'bizniz-skeleton-react')
entry = catalog.stories[0]
capture = StoryCaptureResult(
    story_id=entry.story_id, name=entry.name, title=entry.title,
    screenshot_path=Path('/path/to/captured.png'),  # from Phase 2
    success=True,
)
evaluator = StoryEvaluator(on_status=print)
result = evaluator.evaluate(capture, entry)
print(f'score: {result.overall_score}/10')
print(f'issues: {len(result.issues)}')
print(f'stop: {result.stop_recommendation}')
"
```

Expected: numeric score, JSON-shaped issues list, stop/iterate
recommendation.

### Phase 4 (fix dispatch) — needs claude CLI + low-score eval

Skip in normal flow when score is already high; trigger by passing
a synthetic low-score `StoryEvalResult` with at least one issue.

```bash
PYTHONPATH=. .venv/bin/python -c "
from pathlib import Path
from bizniz.ux_designer.storybook_discovery import discover_stories
from bizniz.ux_designer.storybook_eval import StoryEvalResult, StoryEvalIssue
from bizniz.ux_designer.storybook_fix import StoryFixDispatcher

catalog = discover_stories(Path.home() / 'bizniz-skeleton-react')
entry = catalog.stories[0]
ev = StoryEvalResult(
    story_id=entry.story_id, name=entry.name, title=entry.title,
    overall_score=4,
    issues=[StoryEvalIssue(severity='minor', description='tighten padding')],
    stop_recommendation='iterate',
)
dispatcher = StoryFixDispatcher(on_status=print)
result = dispatcher.dispatch(entry, ev, frontend_root=Path.home() / 'bizniz-skeleton-react')
print(f'status: {result.status}')
print(f'files: {result.files_written}')
"
```

Expected: Coder edits the component file, returns
`status=applied` with at least one file written.

### Phase 5 (score aggregation) — pure-Python, no live deps

```bash
.venv/bin/python -m pytest bizniz/ux_designer/tests/test_storybook_score.py -v
```

### Phase 6 (end-to-end via driver) — requires everything above

```bash
PYTHONPATH=. .venv/bin/python -c "
from pathlib import Path
import tempfile
from bizniz.ux_designer.storybook_driver import StorybookDriver
from bizniz.ux_designer.storybook_eval import StoryEvaluator
from bizniz.ux_designer.storybook_fix import StoryFixDispatcher

evaluator = StoryEvaluator(on_status=print)
fix_dispatcher = StoryFixDispatcher(on_status=print)
driver = StorybookDriver(
    evaluator=evaluator,
    fix_dispatcher=fix_dispatcher,
    on_status=print,
    max_iterations=3,
)
with tempfile.TemporaryDirectory() as d:
    result = driver.run(
        frontend_root=Path.home() / 'bizniz-skeleton-react',
        screenshots_dir=Path(d),
    )
print(f'duration: {result.duration_s:.1f}s')
print(f'score: mean={result.score.mean}, passing={result.score.passing}/{result.score.covered}')
for rec in result.story_records:
    print(f'  {rec.story_id}: final={rec.final_score}, reason={rec.final_stop_reason}')
"
```

Expected: full loop fires, score reported, per-story records
include iteration counts + fix counts.

## Integration into ProUXDesigner

Once Phases 1–6 are validated end-to-end on the skeleton, wire
the driver into the production `ProUXDesigner` by constructing
it with the `storybook_driver` param and passing it through
`v2_build.py`. The hook is already in place in
`pro_ux_designer.py` (after design_lock, before per-route loop)
— enabling it is purely a matter of injecting the driver.

```python
storybook_driver = StorybookDriver(
    evaluator=StoryEvaluator(on_status=on_status),
    fix_dispatcher=StoryFixDispatcher(on_status=on_status),
    on_status=on_status,
    max_iterations=3,
)
ux_designer = ProUXDesigner(
    ...,
    storybook_driver=storybook_driver,
)
```

Defaults to opt-in (None) — existing builds continue running with
per-route-only UX until this is flipped.

## Known limitations / TODO

- The Storybook server is currently spawned as a local subprocess
  (`npm run storybook`). For docker-compose builds where the
  frontend runs in a container, this needs to be wired through
  `docker compose exec frontend npm run storybook` instead. See
  `storybook_server.py` docstring "Phase 2c" note.
- The Playwright capture sidecar JS at
  `bizniz/ux_designer/sidecars/storybook_capture.cjs` is not yet
  exercised end-to-end in pytest — it relies on `node` +
  `playwright` available at invocation time. Manual validation via
  the Phase 2 commands above.
- Per-story re-capture after a fix uses a one-entry catalog;
  efficient for small primitive counts but may benefit from
  batching when story counts grow into the hundreds.
