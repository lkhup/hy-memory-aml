# AML Cycle 2 submission notes

Replace every placeholder and freeze the repository commit before submitting.

## Identity

- System name: `Hy-Memory AML Adapter`
- Version: `0.1.0`
- Division: `Open-source Methods`
- Track: `Textual Memory`
- Repository: `https://github.com/lkhup/hy-memory-aml`
- Fixed commit: `<git commit SHA>`
- Add endpoint: `https://<domain>/add`
- Search endpoint: `https://<domain>/search`
- Health endpoint: `https://<domain>/health`
- Authentication: `Bearer` (a dedicated Memory System Key will be supplied privately)

## Method

The system adapts Tencent Hunyuan's Apache-2.0-licensed Hy-Memory 2.0.3 framework to
the AML Cycle 2 Add/Search contract. It uses Hy-Memory's `pro` chat-memory pipeline,
Chroma vector storage, hybrid retrieval, and a durable verbatim-evidence fallback.
The Add-stage LLM is `gpt-4o-mini`, in accordance with the Open-source Methods rule.

Adapter changes authored for this submission:

- AML request and response schema mapping.
- Constant-time Token/Bearer/X-Api-Key authentication.
- Durable `request_id` idempotency control.
- Strict `user_id` retrieval isolation.
- Hy-Memory channel flattening, deduplication, and rank preservation.
- Durable source-message evidence fallback.
- Bounded concurrency with per-user write serialization.
- A Hy-Memory 2.0.3 shared-client compatibility fix for concurrent LLM calls.
- Direct Uvicorn deployment with persistent Chroma and SQLite storage.

## Attribution

- Hy-Memory framework: Tencent Hunyuan,
  <https://github.com/Tencent-Hunyuan/Hy-Memory>
- Hy-Memory 2.0 technical report: <https://arxiv.org/abs/2610.22196>
- AML contract and evaluation framework:
  <https://github.com/AML-memory/agent-memory-leaderboard>

This repository does not claim authorship of Hy-Memory, AML, their papers, or their
evaluation datasets.

## Operations declaration

- API version: `0.1.0`
- Add concurrency: `16`
- Search concurrency: `16`
- Add timeout: `30 minutes at the edge`
- Search timeout: `<measured p99 and configured value>`
- Rate limits: `<state any infrastructure rate limits>`
- Storage: persistent Chroma, Hy-Memory, and adapter SQLite directories
- Backups: `<describe snapshots/backups>`
- Deployment region: `<region>`
- Operator contact: `<email>`

## Pre-submission checklist

- [ ] Public repository and Apache-2.0 attribution are complete.
- [ ] `.env`, API keys, and database files are absent from Git history.
- [ ] The deployed image matches the fixed commit.
- [ ] `/health` is publicly reachable without authentication.
- [ ] `/add` and `/search` accept the dedicated Memory System Key.
- [ ] Repeating an identical `request_id` does not write twice.
- [ ] Reusing a `request_id` with a different payload returns HTTP 409.
- [ ] Search never retrieves evidence from another `user_id`.
- [ ] Add returns only after the write is searchable.
- [ ] Local tests and deployment smoke pass.
- [ ] Load test passes at the concurrency declared to AML.
- [ ] AML public Smoke passes before starting Full.

