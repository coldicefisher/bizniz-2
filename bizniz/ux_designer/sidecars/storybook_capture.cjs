/**
 * Storybook screenshot sidecar — Phase 2b.
 *
 * Reads a JSON capture plan from stdin, navigates a Playwright
 * Chromium instance to each story's iframe URL, screenshots, and
 * emits one JSON status line per story on stdout. PNGs land on
 * disk at the plan's specified output_path.
 *
 * Plan shape (matches bizniz/ux_designer/storybook_capture.py
 * CapturePlan):
 *
 *   {
 *     "storybook_base_url": "http://localhost:6006",
 *     "output_dir": "/abs/path",
 *     "viewport_width": 1280,
 *     "viewport_height": 720,
 *     "wait_after_load_ms": 600,
 *     "stories": [
 *       {
 *         "story_id": "common-toast--default",
 *         "name": "Default",
 *         "title": "Common/Toast",
 *         "url": "http://localhost:6006/iframe.html?id=common-toast--default&viewMode=story",
 *         "output_path": "/abs/path/common-toast--default.png"
 *       },
 *       ...
 *     ]
 *   }
 *
 * Status line shape (one per story, on stdout):
 *
 *   {"story_id": "...", "success": true, "output_path": "...", "duration_ms": 837}
 *   {"story_id": "...", "success": false, "error": "navigation timeout"}
 *
 * Exit code 0 if Playwright launched cleanly (per-story failures
 * do NOT fail the process — they're reported in the status lines).
 * Exit code 1 if Playwright itself crashed or the plan was invalid.
 */
"use strict";

const fs = require("fs");
const path = require("path");

async function readStdinJson() {
  return new Promise((resolve, reject) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { buf += chunk; });
    process.stdin.on("end", () => {
      try { resolve(JSON.parse(buf)); }
      catch (e) { reject(new Error(`bad plan JSON: ${e.message}`)); }
    });
    process.stdin.on("error", reject);
  });
}

function emit(record) {
  process.stdout.write(JSON.stringify(record) + "\n");
}

async function captureOne(page, story, waitAfterLoadMs) {
  const t0 = Date.now();
  try {
    await page.goto(story.url, {
      waitUntil: "networkidle",
      timeout: 30000,
    });
    // Settle whatever rendered. Storybook's iframe runs Vite HMR
    // during dev — a short wait avoids capturing mid-paint flicker.
    await page.waitForTimeout(waitAfterLoadMs || 600);
    // Ensure output dir exists.
    fs.mkdirSync(path.dirname(story.output_path), { recursive: true });
    await page.screenshot({ path: story.output_path, fullPage: false });
    return {
      story_id: story.story_id,
      success: true,
      output_path: story.output_path,
      duration_ms: Date.now() - t0,
    };
  } catch (e) {
    return {
      story_id: story.story_id,
      success: false,
      error: `${e.name || "Error"}: ${e.message}`,
      duration_ms: Date.now() - t0,
    };
  }
}

async function main() {
  const plan = await readStdinJson();
  if (!Array.isArray(plan.stories) || plan.stories.length === 0) {
    process.stderr.write("plan has zero stories\n");
    return 0;
  }
  // Lazy-require so a missing playwright dep gives a clear error.
  let chromium;
  try {
    chromium = require("playwright").chromium;
  } catch (e) {
    process.stderr.write(
      `playwright not installed in sidecar runtime: ${e.message}\n`
    );
    return 1;
  }
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      viewport: {
        width: plan.viewport_width || 1280,
        height: plan.viewport_height || 720,
      },
    });
    const page = await context.newPage();
    for (const story of plan.stories) {
      const record = await captureOne(page, story, plan.wait_after_load_ms);
      emit(record);
    }
    await context.close();
  } finally {
    await browser.close();
  }
  return 0;
}

main()
  .then((code) => process.exit(code || 0))
  .catch((e) => {
    process.stderr.write(
      `storybook_capture sidecar fatal: ${e.stack || e.message}\n`
    );
    process.exit(1);
  });
