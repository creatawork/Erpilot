# Erpilot Showcase Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create and publicly deploy a concise, evidence-grounded Erpilot project showcase through Sites.

**Architecture:** Build a standalone, buildless HTML/CSS/JavaScript site in a dedicated Site source directory, with no dependency on the ERP API. Sites source tooling owns Git preparation, push and archive packaging; the native Sites tools own registration, public access, version saving and deployment.

**Tech Stack:** Sites, semantic HTML, CSS, vanilla JavaScript.

**Spec:** [Erpilot project showcase site design](../specs/2026-10-09-erpilot-showcase-site-design.md)

## Global Constraints

- Keep the Site source separate from the existing application's source and uncommitted work.
- Use only verified repository and dated acceptance-report facts; label the scripted approval flow as simulated.
- The public Site must not expose application credentials, evaluation traces, business data, or an ERP/API proxy.
- Persist and reuse the exact Sites `project_id`; create the Site only once.
- Push source before saving a version; the archive and full `commit_sha` must match that pushed state.
- Publish a saved version using public Site deployment and confirm success before reporting a live URL.

## Review Focus

- Simulated approval could be mistaken for a real ERP write; pin this in persistent page copy and keep all interactions local.
- Planned RAG, multi-model comparison, evaluation curves, and public demo could be mistaken for shipped features; label roadmap state beside each item.
- Recovery evidence could be overgeneralized; state R01–R06 process matrix 19/19 and R07–R10 deterministic component coverage separately, with report dates.
- Narrow viewports or keyboard-only use could hide sections or trap focus; check navigation, demo controls, contrast, and reduced-motion behavior at desktop and mobile widths.
- Public publication could be blocked by the new Site's private default; explicitly set and verify public access before deployment.

---

### Task 1: Register an isolated Site source

**Files:**
- Create: `sites/erpilot-showcase/.openai/hosting.json`
- Create: `sites/erpilot-showcase/` as a dedicated standalone Site source directory

**Interfaces:**
- Consumes: the approved design, the exact user-requested Sites registration response, and its short-lived source repository credential.
- Produces: a dedicated source checkout with the returned `project_id` and static directory `dist`.

- [ ] **Step 1: Create and attach an isolated worktree** from the known current branch `codex/crash-recovery-reconciliation`; use its returned workspace path for all later repository actions so existing uncommitted files are untouched.
- [ ] **Step 2: Check for existing Site identity** at `sites/erpilot-showcase/.openai/hosting.json`; if the directory is new, confirm it has no Site ID and inspect `git status` before registration.
- [ ] **Step 3: Register once with Sites** using slug `erpilot-showcase`, title `Erpilot | 会请示的 ERP 智能体`, and the approved project description. Preserve the returned `id`, `expected_url`, repository credential, and registration response in session memory; never print or save the token.
- [ ] **Step 4: Persist the returned ID immediately** with the installed Sites `set-project-id.mjs` helper in the dedicated source directory; if the credential is absent, request a fresh credential for the same returned ID instead of registering again.
- [ ] **Step 5: Open/prepare the dedicated Site source** with `site-workflow.mjs` and the returned credential, following its hidden-stdin JSON protocol; verify its final checkout path and that only the standalone Site directory is the publishing root.
- [ ] **Step 6: Read back `.openai/hosting.json` and Site metadata** to confirm the exact ID and `static.directory: "dist"` are retained before adding page files.

### Task 2: Build the visitor story and simulated approval demo

**Files:**
- Create: `sites/erpilot-showcase/dist/index.html`
- Create: `sites/erpilot-showcase/dist/site.css`
- Create: `sites/erpilot-showcase/dist/site.js`

**Interfaces:**
- Consumes: the Site's `expected_url`, approved design, README, next-phase plan, and dated acceptance reports.
- Produces: a single responsive static page with section navigation and a local-only approval simulation.

- [ ] **Step 1: Verify every planned public fact** against the current README, `tasks/plan-next-phase.md`, `reports/acceptance/2026-10-07-baseline.md`, and `reports/acceptance/2026-10-08-crash-recovery.md`; cite source titles and section names beside claims, link only to a verified public URL, and omit anything that cannot be traced.
- [ ] **Step 2: Write semantic page structure** for hero, simulated interaction, architecture, evidence, roadmap, and footer; use the generated Site origin only for absolute URLs that need it and add a source-repository link only if its public URL is verified.
- [ ] **Step 3: Add the scripted interaction** so approval and rejection update an explicitly simulated result in the page; do not make network requests or call project tools.
- [ ] **Step 4: Style the page** in the approved editorial visual direction with responsive layout, visible focus states, readable contrast, and reduced-motion support.
- [ ] **Step 5: Check the page source and package boundary** to ensure no `.env`, credentials, traces, business database, or unrelated repository content has been copied into `dist`.

### Task 3: Preview and review the published experience locally

**Files:**
- Verify: `sites/erpilot-showcase/dist/index.html`
- Verify: `sites/erpilot-showcase/dist/site.css`
- Verify: `sites/erpilot-showcase/dist/site.js`

**Interfaces:**
- Consumes: the first recognizable static build and the Sites local-preview guidance for this host.
- Produces: a manually reviewed page and a source state ready for packaging.

- [ ] **Step 1: Read the Sites local-preview guide** before starting a preview; serve only the dedicated `dist` directory using the documented local preview path.
- [ ] **Step 2: Show the first recognizable page** in the visible browser preview and keep it current while reviewing remaining sections.
- [ ] **Step 3: Review desktop and narrow/mobile layouts** for section order, overflow, evidence legibility, navigation and footer links.
- [ ] **Step 4: Use keyboard-only navigation** through the menu and demo; confirm focus remains visible, approve/reject labels are announced clearly, and the demo result is understandable without motion.
- [ ] **Step 5: Exercise both demo decisions** and inspect browser activity to confirm neither action sends a request to the ERP/API or mutates external state.
- [ ] **Step 6: Recheck all metrics, dates and roadmap labels** against the referenced reports, then inspect the final diff and Site source directory contents.

### Task 4: Package, set public access, and deploy

**Files:**
- Verify: `sites/erpilot-showcase/.openai/hosting.json`
- Package: an absolute archive path returned by `site-workflow.mjs` or specified by its packaging input

**Interfaces:**
- Consumes: the final manually reviewed files, the complete prior Site source result, and a credential valid for the same Site.
- Produces: one saved public Site version and a confirmed production URL.

- [ ] **Step 1: Package the buildless static output** through `site-workflow.mjs` using `commands: []` and an absolute `archivePath`; retain the helper's verified `project_id`, full `commit_sha`, and archive unchanged.
- [ ] **Step 2: Confirm the source push** succeeded and the reported commit is the current head of the Site's configured source branch; stop before save if either value does not match.
- [ ] **Step 3: Set Site access to `public`** with `update_site_access`, then read Site/access state to confirm anyone with the link can visit it.
- [ ] **Step 4: Save exactly one version** with `save_site_version`, passing the verified project ID, full commit SHA, and archive.
- [ ] **Step 5: Deploy that saved version** with `deploy_site_version`, passing the exact saved version ID; never deploy an unsaved local build.
- [ ] **Step 6: Check deployment status only if it is pending/building/publishing**, stop on a terminal result, and confirm the returned production URL and public access.
- [ ] **Step 7: Open the live Site** in the existing Codex browser panel when supported and verify the live landing page and simulated approval path; report the URL and any remaining verification limit.
