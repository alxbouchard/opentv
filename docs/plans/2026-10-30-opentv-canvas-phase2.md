# OpenTV Canvas Phase 2 Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Integrate the approved Canvas hybrid into the real Raspberry Pi OpenTV product with safe owner confirmation, reversible scene persistence, animated ambient rendering and optional per-agent customization—without weakening playback, Muse, CEC, setup or remote-control contracts.

**Architecture:** Preserve the immutable OpenTV shell and existing navigation. Add a root-owned/product-managed declarative Canvas state with a closed schema, in-memory previews and five-version history. The TV page renders only enumerated local presets; agent requests go through the Unix Agent Port/MCP with a separate optional `canvas` capability and can only propose a preview. The TV owner confirms or rejects on-screen with `Non` focused by default. Muse remains exactly 11 commands and stays connected; Canvas tools are exposed only to identities explicitly granted the optional capability.

**Tech Stack:** Python 3.11 stdlib, current OpenTV Agent Port/MCP, systemd sandbox, ES5 JavaScript, CSS transform/opacity animation, unittest, Playwright and Acorn.

---

## Fixed product decisions

- Production default for a device with no prior Canvas state is the approved `canvas` hybrid scene.
- Visual surface can change; security shell cannot: setup, QR, Settings, confirmations, remote semantics, playback overlay, offline screen, CEC and controller gates remain immutable.
- Scene schema v1 is closed: `wallpaper`, `motion`, `accent`, `shade`, `hero_focus`, `row_order`. No arbitrary text, URL, HTML, CSS, JavaScript or remote asset.
- Built-in presets: `mist`, `cinema`, `canvas`; `canvas` is the default. `motion` supports `off`, `calm`, `alive`.
- Preview automatically expires; any human key or busy transition cancels it. Confirming requires the TV sheet and explicit `Oui`; `Non` is focused first. Agent calls never confirm.
- Animation pauses during setup, any panel, playback/loading/closing/returning, offline state, hidden document, reduced motion or degraded performance.
- No video, worker, filter, backdrop-filter or blur. At most four continuous layers, transform/opacity only.
- Keep Muse at exactly 11 commands. Do not change `MUSE_SCOPES` or `LINKED_REQUIRED`. Optional Canvas tools are dynamically visible only to identities with `canvas` permission; the linked Muse identity remains healthy without it.
- Every tracked product-file change invalidates playback promotion. Deployment must repromote only after the real HDMI player canary passes again.

## Task 1: Production Canvas domain and persistence

**Files:**
- Create `scripts/pi4/opentv_pi/canvas.py`
- Create `scripts/pi4/tests/test_canvas.py`
- Modify `scripts/pi4/opentv_pi/config.py` only for explicit Canvas paths/config if required

**Acceptance:**
- Port the reviewed model semantics from the prototype, adapting to OpenTV path/ownership helpers.
- Fixed schema/default/presets, canonical hash/revision, preview IDs and monotonic expiry.
- Atomic applied state and history under `/var/lib/opentv/canvas`, last five revisions, rollback/default.
- Corrupt/unsafe/symlink/FIFO/hardlink/oversize state fails closed to the immutable default; only explicit owner reset replaces it.
- Cross-process lock; reload under lock; confirm rechecks expiry under lock.
- Human/busy event cancels previews. Nested busy reasons are represented safely, not a single lossy state.
- TDD under Python 3.13 and 3.11.

## Task 2: TV API and owner confirmation lifecycle

**Files:**
- Modify `scripts/pi4/opentv_pi/app.py`
- Modify `scripts/pi4/opentv_pi/web.py`
- Extend Canvas tests and relevant web/app tests

**Acceptance:**
- `/api/tv/state` includes a scrubbed Canvas state: applied scene/revision/hash, optional preview and stable status; never paths or agent credentials.
- Agent preview creates a TV-visible proposal; TV POST endpoints can confirm, cancel, reset and owner rollback using the existing TV key/origin/peer protections.
- Confirmation sheet identifies the proposing canonical agent, shows a concise description, focuses `Non`, times out safely and never appears during setup, assistant management, Muse setup, playback or returning.
- Any human navigation cancels a live preview before another action; no row rebuild beneath a panel or player.
- Audit records enum values, agent, revision/hash/result only.

## Task 3: Real Canvas renderer and Settings

**Files:**
- Add `scripts/pi4/ui/canvas.js`
- Modify `scripts/pi4/ui/tv.html`, `tv.css`, `tv.js`
- Modify static allowlist/CSP only as needed for the local script
- Add/extend real-browser Canvas E2E tests

**Acceptance:**
- Port the approved hybrid aesthetic into the actual v23 shell, not a parallel dashboard.
- Default Canvas appearance is visibly richer than v23: ambient aurora/brume, focus-responsive hero glow, deeper cards and scene pill/recommendation reason while preserving couch-distance readability.
- No remote assets. Existing QR and all panels remain unstyled by agents.
- ES5 parse passes. At most four animated layers; transform/opacity only.
- Pause/degrade/reduced-motion behavior is executable and tested in real Chromium at 1920×1080.
- Settings gains an owner-only `Ambiance OpenTV` entry for three presets, motion mode, previous version and original/default reset.
- Screenshots captured for populated and sparse states; geometry checks keep focus rings, QR, hero, panels and footer clear.

## Task 4: Optional per-agent Canvas capability and tools

**Files:**
- Create `scripts/pi4/opentv_pi/canvas_tools.py`
- Modify `validate.py`, `agentport.py`, `mcp.py`, `agents.py`, `app.py` and agent-management UI/tests as required
- Do not modify Muse command descriptions/count/scopes except tests proving they remain unchanged

**Acceptance:**
- New optional `canvas` permission, absent by default and separate from nested read/feedback/display/control levels.
- Manage Assistants shows `Changer l'ambiance OpenTV` separately per agent; unchanged policy posts remain `keep`.
- MCP dynamically exposes closed tools only to identities with `canvas`: status, preview and cancel/rollback proposal as designed. No raw scene object; flat enums/known preset IDs only.
- Agent preview rate: at most one per 5 seconds and 20 per 10 minutes; owner/human actions always win.
- Audit and canonical linked-agent naming work, but `muse-device` without Canvas stays READY and exactly 11 Muse commands remain.
- No agent can confirm its own preview, change Settings/QR/playback, or bypass TV owner consent.

## Task 5: Packaging, lifecycle, version and documentation

**Files:**
- Modify `scripts/pi4/install.sh`, uninstall/tmpfiles/manifest-related files as required
- Modify `scripts/pi4/opentv_pi/__init__.py`
- Modify `scripts/pi4/README.md`
- Update tests and Canvas handoff docs

**Acceptance:**
- Version becomes `0.7.0` only after feature tests are green.
- Installer ships the new reviewed files and creates safe Canvas state directories; upgrade preserves state; uninstall without purge preserves user scene, purge removes it.
- All non-Canvas units keep the Canvas state inaccessible unless they need it.
- README documents owner flow, agent permission, safe mode, recovery, performance budget and exact field checks.
- Production manifest and playback-promotion invalidation behavior are explicit and tested.

## Task 6: Verification, review and real-Pi delivery

**Acceptance:**
- Targeted Canvas/backend/UI/MCP/Muse/playback/install tests pass on Python 3.13 and 3.11 with real Playwright/Acorn.
- Full Pi suite passes on both interpreters, or any unrelated host failure is reproduced on the baseline and reported honestly.
- Two independent reviews in order: specification compliance, then quality/security; all Critical/Important findings fixed and re-reviewed.
- Commit and push `main`; local and `origin/main` exact SHA match.
- Before deployment: durable Pi backup and archive hash match.
- Install on `opentv-salon.local`, reboot, health, manifest, services, boot, catalogue, CEC and Muse all pass.
- Run real Canvas performance canary: 1080p, frame cadence, Chromium/Cage CPU/RSS, temperature, `get_throttled=0x0`, stable `hvs_underrun`; pause animation during playback.
- Repromote playback only after real player open/close/same-card canary succeeds.
- Exercise owner confirmation and rollback on the real TV; verify agent without Canvas is refused and an explicitly authorized test identity can preview but not confirm.
- Ask Alex for final couch-distance visual confirmation/photo before declaring the visual field gate complete.
