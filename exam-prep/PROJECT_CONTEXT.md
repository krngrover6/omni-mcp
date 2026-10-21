# Project Context: NCP-OUSD exam prep

## Goal

Prepare the user for **NVIDIA-Certified Professional: OpenUSD Development (NCP-OUSD)**, confirmed by the user on 2026-10-20. The shared MCP router provides the tools: blueprint, weighted study plan, original quizzes, composition sandbox, offline USD knowledge, and saved progress with spaced repetition.

Exam facts, from NVIDIA's pages and the October 2025 study guide, checked on 2026-10-20:
- 60–70 multiple-choice questions, single-answer and multi-select, many built on USDA snippets.
- Online and remotely proctored through Certiverse. $200 USD, English, pass/fail with no published passing score.
- Time limit: 120 minutes on the US page. One paragraph on the UK and EU pages says 90, so check when registering.
- 14-day wait before a retake, at most 5 attempts in 12 months. Valid for 2 years.
- USD Composer is **not** examined. Questions are at the OpenUSD/USDA/API level, so use Composer only to see concepts visually.

Domain weights: Composition 23%, Data Exchange 15%, Pipeline Development 14%, Data Modeling 13%, Debugging and Troubleshooting 11%, Content Aggregation 10%, Visualization 8%, Customizing USD 6%. The guide numbers 55 objectives, but 11 appear under two domains, so there are 44 distinct topics. That count was checked against the guide on 2026-10-20; an earlier note of "46" was wrong.

## Independence

This project reuses the shared router in `C:\Users\krngr\Omniverse\omniverse_scene_mcp`. It is separate from the ZAZ, bag, factory and Isaac mentorship projects, and never changes their scenes.

## Entry points

- Project folder: `C:\Users\krngr\Omniverse\exam-prep`
- Progress store: `exam-prep\progress.json`, written only by the router's exam tools. Override with `OMNIVERSE_EXAM_PREP_DIR`.
- Code: `omniverse_scene_mcp/src/omniverse_scene_mcp/exam/` (blueprint, progress, spaced repetition, sandbox, question bank), `offline_knowledge.py`, `usd_insight.py`, `usd_validate.py`
- Official study guide (current link): https://dam-cdn.nvd.orangelogic.com/AssetLink/sjrg2vjhic7285sph487x53f5e54erf7.pdf. The older nvdam.widen.net link in `LearnOpenUSD-main/docs` returns 404.

## Assets and licensing

| Asset | Source | License | Local path | Notes |
|---|---|---|---|---|
| Question bank | Written for this project | Original | `omniverse_scene_mcp/.../exam/data/` | Never copy NVIDIA's sample questions or third-party exam "dumps" |
| Objectives text | NVIDIA study guide | NVIDIA copyright | Paraphrased only | Cite the guide URL |
| Offline USD docs | kit-usd-agents usd_code data v25.11 | See that repository | `kit-usd-agents/source/aiq/usd_code_fns/.../data/v25.11` | Read-only; never modify the submodule |
| Learn OpenUSD docs | NVIDIA LearnOpenUSD | Apache-2.0 | `LearnOpenUSD-main/docs` | Cite by file and line |

## UI and MCP behavior

- Exam tools are read-only except the progress store, which is written to this folder only.
- Insight and validation tools in Tier 1 read **saved files**, never the live stage, and never save.
- Tests use temporary directories and never touch `progress.json` or any user scene.

## Decisions and lessons

- 2026-10-20: The user chose NCP-OUSD, a Tier 1 router-side build first, this folder for progress, and the offline USD knowledge before the Docker/cloud USD docs helper on port 9903.

## Current status and next steps

- Tier 1 was built on branch `feat/ncp-ousd-tier1` and committed as `832a901`.
  - It adds 18 tools and a bank of 100 original questions.
  - An adversarial review confirmed 42 findings, and all 42 are fixed. 305 of 305 tests passed offline.
  - It has been live since 2026-10-20, when the router restarted as PID 6864. Read-only checks passed for `router_health`, the blueprint, a quiz, `usd_docs_search` and `scene_stage_info` on `session02/main.usda`. No progress has been recorded yet.
- Tier 2 was integrated on 2026-10-21 on the same branch (not committed yet).
  - An adversarial review confirmed 47 findings. 43 were applied; the other 3 were question removals the fixer kept, because the duplicate each one flagged had already been rewritten.
  - 434 of 434 tests pass offline.
  - Question bank: 233 original questions after the review removed duplicates. Domain shares are within 1.2 points of the exam weights.
  - Daily lessons: `learning_exam_day`, `learning_exam_day_complete` and `learning_exam_set_date`. The plan settings live in `progress.json`. `router_dispatch` answers "start day 3", "day 1" and "next lesson".
  - Timed mock exam: `learning_mock_exam_start`, `_questions`, `_answer`, `_flag`, `_status`, `_finish` and `_abandon`. Sessions are saved in `mock_sessions.json` in this folder. Finishing records the answered questions as progress. "mock exam" in `router_dispatch` only explains the steps and never starts the clock.
  - Live insight: the six insight tools take `mode` (`auto`, `live` or `saved_file`), and every result says which mode it used. `auto` falls back to the saved file with a note when the bridge cannot answer. `scene_capabilities` reports what the bridge supports.
  - Authoring: `scene_create_light`, `scene_create_camera` and `scene_aim`. They are dry runs by default; applying changes the open scene in memory.
  - Live since 2026-10-21 02:37–02:45.
    - The user ran `reload_mcp_bridge.py` in Isaac. All 31 self-tests passed on Isaac's USD, and the bridge reports rpc_version `tier2-1`.
    - The first attempt kept the old bridge. Isaac had started before `kit_rpc.py` existed, so both Isaac scripts now call `importlib.invalidate_caches()` first.
    - The router was restarted as PID 50604.
  - Read-only checks passed:
    - `scene_capabilities` reports live insight and authoring as available.
    - Live `scene_explain_value` on MainLight returned `inputs:intensity` = 4000 (local opinion). It also reported that main.usda has unsaved edits in this Isaac session.
    - A dry-run rect light aimed at the Shelves had 0° aim error and wrote nothing: the prim count stayed 49 and the prim does not exist.
    - "start day 1" routes to the daily lesson.
  - `learning_exam_day` was not called, because its first call saves the plan start date.
- Next: the user sets an exam date (`learning_exam_set_date`) or says "start day 1". Commit the Tier 2 work when the user asks.
