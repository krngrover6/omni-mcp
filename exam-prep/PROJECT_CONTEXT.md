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

- Tier 1 was built on branch `feat/ncp-ousd-tier1` (not committed yet).
  - It adds 18 tools and a bank of 100 original questions.
  - An adversarial review confirmed 42 findings, and all 42 are fixed. 305 of 305 tests pass offline.
  - It has been live since 2026-10-20, when the router restarted as PID 6864. Read-only checks passed for `router_health`, the blueprint, a quiz, `usd_docs_search` and `scene_stage_info` on `session02/main.usda`. No progress has been recorded yet.
- Next: Tier 2 live-scene insight (needs an Isaac bridge reload), a timed mock exam, and auto-checked drills.
