# OpenTV Canvas Phase 1 Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Deliver three executable 1920×1080 OpenTV Canvas prototypes plus a tested, local-only typed scene model without changing the installed Pi product or production manifest.

**Architecture:** Keep the production `scripts/pi4/ui/`, `opentv_pi/`, install lists, Muse bridge, Agent Port and manifest untouched. Put the complete visual lab and reference model under `scripts/pi4/prototypes/canvas/`; add prototype-only tests under the normal Pi test directory. The lab uses a fixed shell, closed scene presets and ES5 JavaScript; no arbitrary HTML/CSS/JS, URL, remote asset, video, worker, filter or blur is accepted.

**Tech Stack:** Static HTML/CSS/ES5 JavaScript, Python 3.11+ stdlib, Node + Playwright/Acorn test harness already present in the repo.

---

## Product contract

- Three real variants: `mist` (Brume vivante), `cinema` (hero artwork emphasis), and `canvas` (recommended hybrid).
- Preserve OpenTV spatial invariants in every variant: 1920×1080, clock/date top-left, labelled 72 px Settings control top-right, row navigation visual model, 300 px phone-remote column with 232 px QR, fixed footer help, no overlap.
- Animation uses only `transform` and `opacity`, at most four continuously animated layers, with `prefers-reduced-motion` and an explicit motion-off state.
- Pause animation for setup, panel, playback/loading, offline, hidden document and degraded performance states.
- Prototype scene inputs are closed enums only: wallpaper, motion, accent, shade, hero focus and row order. Unknown keys/values, URLs, CSS and arbitrary asset names fail closed.
- Preview is temporary and auto-reverts; apply requires explicit confirmation; keep five revisions; rollback to previous/default; corrupt persistence fails closed to the immutable OpenTV default.
- Phase 1 never adds a Muse command or scope and never touches production files or the installed manifest.

## Task 1: Implement the typed prototype model with TDD

**Files:**
- Create `scripts/pi4/prototypes/canvas/canvas_model.py`
- Create `scripts/pi4/tests/test_canvas_prototype.py`

**Steps:**
1. Write failing tests for closed enums, unknown-key rejection, URL/CSS rejection, canonical hash/revision, preview expiry, explicit confirmation, five-version history, rollback, atomic persistence and corrupt-store fail-closed.
2. Run only `test_canvas_prototype.py` and observe failures.
3. Implement a stdlib-only reference model with injectable wall/monotonic clocks and directory-local atomic writes/fsync.
4. Rerun targeted tests under Python 3.13 and Python 3.11.

## Task 2: Build the executable visual lab

**Files:**
- Create `scripts/pi4/prototypes/canvas/lab.html`
- Create `scripts/pi4/prototypes/canvas/lab.css`
- Create `scripts/pi4/prototypes/canvas/canvas.js`
- Create `scripts/pi4/prototypes/canvas/README.md`

**Steps:**
1. Build a self-contained OpenTV-like shell with realistic fake catalogue data and visible variant controls intended only for the lab.
2. Implement `mist`, `cinema`, and `canvas` using closed preset data in ES5.
3. Implement motion-off, pause/degraded state, variant switching, preview countdown, apply, undo and immutable-default reset demonstrations.
4. Ensure no external resource, secret, URL input, raw CSS/HTML editor or arbitrary script path exists.
5. Document how to run and what is intentionally not production wiring.

## Task 3: Add real-browser acceptance and screenshots

**Files:**
- Create `scripts/pi4/tests/e2e_canvas_prototype.js`
- Generate deliverables under `.artifacts/opentv-canvas/` (ignored/untracked): `mist.png`, `cinema.png`, `canvas.png`.

**Steps:**
1. Serve the prototype locally with a bounded stdlib HTTP server.
2. Parse `canvas.js` with the existing Acorn module at ES5.
3. Run Playwright at 1920×1080 for all three variants.
4. Assert shell geometry, QR/card/panel non-overlap, text size floor, animation-property allowlist, pause behavior, reduced/off motion, invalid preset refusal, preview reversion and undo/reset.
5. Capture one screenshot per variant.

## Task 4: Verify non-impact and review

**Steps:**
1. Prove the production manifest inputs and installed file lists are unchanged relative to `a64e780`.
2. Run targeted Python 3.13 and 3.11 tests, the Canvas Playwright scenario and Acorn ES5 parse.
3. Run the existing remote/layout tests most likely to detect accidental coupling.
4. Run `git diff --check`; confirm `scripts/pi4/ui/`, `opentv_pi/`, install scripts and Muse files are byte-identical to `a64e780`.
5. Complete spec-compliance review, then code-quality/security review.
6. Commit and push `main` only after both reviews pass. Do not deploy the prototype to the Pi.
