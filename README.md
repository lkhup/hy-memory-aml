# Hy-Memory AML Adapter

An open-source, self-hosted adapter that exposes Tencent Hy-Memory 2.0 through the
Agent Memory Leaderboard (AML) Cycle 2 Textual Memory `Add` and `Search` contract.

The service is intentionally limited to memory ingestion and evidence retrieval.
It never generates the benchmark's final answer.

## Architecture

```text
AML evaluator
    │ HTTP + Memory System Key
    ▼
Uvicorn ──► FastAPI adapter ──► Hy-Memory 2.0.3 ──► Chroma
                           ├──► Add-stage LLM API
                           └──► Embedding API
```

The adapter provides:

- `GET /health` without authentication;
- `POST /add` with durable `request_id` idempotency;
- `POST /search` with strict `user_id` isolation;
- Bearer, Token, and `X-Api-Key` authentication;
- conversion of Hy-Memory's retrieval channels to AML memory records;
- a durable verbatim-evidence fallback for exact facts and auditability;
- persistent Chroma, Hy-Memory, and adapter SQLite data;
- a single Uvicorn worker with bounded Add/Search concurrency.

## Competition compliance

For AML Cycle 2 **Open-source Methods**, configure Hy-Memory's Add-stage LLM as
`gpt-4o-mini`. The adapter defaults shown in `.env.example` follow that rule. The
Hy-Memory-30B-A3B writer model should not be substituted in an Open-source Methods
Full run unless the AML organizers approve it in writing. Commercial Products have
different model restrictions and are not prize-eligible.

Always re-check the current official documents before a formal run:

- [AML API Guide](https://agentmemoryleaderboard.ai/api-guide)
- [AML Rules](https://agentmemoryleaderboard.ai/rules)
- [Hy-Memory](https://github.com/Tencent-Hunyuan/Hy-Memory)

## Local development

Requirements:

- Python 3.10–3.13
- [`uv`](https://docs.astral.sh/uv/)
- valid OpenAI-compatible LLM and embedding credentials for real Hy-Memory calls

Install and test:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check app tests
```

For a local API using the default embedded storage, copy the environment template
and change the secrets and model endpoints:

```bash
cp .env.example .env
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The submitted deployment uses Chroma, matching `.env.example`. Docker/Compose
files are retained as an optional deployment variant, but were not used for the
Cycle 2 Full run described here.

## Server deployment

Recommended baseline when LLM and embedding inference use remote APIs:

- Ubuntu 22.04 or 24.04;
- 8 vCPU, 16 GiB RAM minimum (32 GiB preferred);
- 100 GiB SSD;
- Python 3.10 with the `venv` module or another isolated environment;
- an inbound TCP port dedicated to the API.

### 1. Prepare configuration

```bash
git clone <your-public-repository-url> hy-memory-aml
cd hy-memory-aml
cp .env.example .env
```

Generate a dedicated API authentication secret:

```bash
openssl rand -hex 32
```

Put it in `API_TOKEN`, then configure the LLM and embedding credentials.
Never commit `.env`.

### 2. Create the environment and start the service

```bash
python3 -m venv .venv-deploy
.venv-deploy/bin/pip install -e .
mkdir -p logs
nohup .venv-deploy/bin/python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 18793 --workers 1 \
  >> logs/app.log 2>&1 < /dev/null &
echo $! > app.pid
```

Use the host firewall or provider security group to expose only the intended API
port. Put a reverse proxy in front only when TLS or a domain is required.

### 3. Verify the deployed API

Linux/macOS:

```bash
./scripts/smoke.sh https://memory.example.com "$API_TOKEN"
```

PowerShell:

```powershell
./scripts/smoke.ps1 -BaseUrl https://memory.example.com -Token $env:API_TOKEN
```

Also test all AML properties before submission:

- the same `request_id` and payload returns success without a second Hy-Memory write;
- the same `request_id` with a changed payload returns HTTP 409;
- a different `user_id` cannot retrieve the first user's evidence;
- process and server restarts preserve all data;
- the service sustains the Add/Search concurrency declared in the AML request;
- `/search` returns evidence, never a final answer.

### 4. Back up persistent data

Back up the configured `MEMORY_DATA_DIR`, `MEMORY_PERSIST_DIR`, `DATA_DIR`, and
fast-memory fallback directory. Do not replace or remove them while an evaluation
is active.

### 5. Upgrade safely

Freeze the commit used for the formal evaluation. Do not deploy code or configuration
changes during a Full run. Before an upgrade, snapshot all persistent directories,
run smoke against a separate staging instance, and then switch.

## API examples

Add:

```bash
curl -X POST https://memory.example.com/add \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "request_id":"eval:run_abc:locomo:conv-0:chunk-0",
    "messages":[{
      "role":"user",
      "content":"I live in Singapore.",
      "timestamp":1704067200000
    }],
    "user_id":"eval:run_abc:locomo:conv-0",
    "session_id":"eval:run_abc:sample:0"
  }'
```

Search:

```bash
curl -X POST https://memory.example.com/search \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "query":"Where does the user live?",
    "options":["A. Singapore","B. Tokyo"],
    "user_id":"eval:run_abc:locomo:conv-0",
    "top_k":100
  }'
```

## Configuration notes

- `MEMORY_MODE=pro` enables Hy-Memory extraction and reconciliation without the
  operational cost of the full Ultra graph pipeline.
- `HY_MEMORY_READER=hybrid` combines Hy-Memory retrieval channels over Chroma.
- `MEMORY_EMBEDDING_DIMS` must exactly match the embedding model's output.
- `RAW_EVIDENCE_LIMIT` controls how many relevant source messages may supplement
  Hy-Memory's semantic results.
- `INCLUDE_OPTIONS_IN_QUERY` defaults to `false`; enable it only after evaluating
  whether multiple-choice option text improves retrieval without adding noise.
- Run one API worker. The adapter handles bounded concurrency within that process.
  Writes for one user are serialized; independent users can write concurrently.
  Searches do not take the write lock: wait for Add to succeed before querying
  data from that write.

### LLM concurrency fix (2026-10-21)

`app/hy_transport.py` installs a version-guarded compatibility patch for the
installed Hy-Memory 2.0.3 LLM transport. A failing request must not close the
cached HTTP client while other requests are still using it. The patch keeps
the shared pool alive until shutdown, preserving prompts, tools, extraction,
reconciliation, and the existing outer retry policy.

The deployed configuration uses `MAX_ADD_CONCURRENCY=16`,
`MAX_SEARCH_CONCURRENCY=16`, `MEMORY_LLM_GLOBAL_CONCURRENCY=16`,
`MEMORY_MODE=pro`, and `FAST_MEMORY_ENABLED=false`. All new writes finish
the synchronous Hy-Memory pipeline before acknowledgement. The transitional
`FAST_MEMORY_READ_FALLBACK=true` reads previously indexed fast-mode records;
it does not convert those historical records into LLM-extracted memories.

An isolated synthetic benchmark (16 distinct users, same messages, raw-evidence
fallback disabled) took 68.645 seconds with concurrency 2 and 28.856 seconds
with concurrency 16: about 2.38x batch throughput. Both returned 16/16 successful
writes and 16/16 semantic hits. A second 16-way run also completed 16 initial
writes, 16 subsequent updates, and 16 searches finding the updated fact.
This is a small smoke/performance test, not an official Full result or a score
guarantee. Provider 429/502 responses can still require retries. A single user's
writes remain serialized to protect memory consistency.

Reproduce on isolated storage using `scripts/bench_hy_llm.py --env-file .env
--concurrency 16 --count 16 --rounds 2`. It consumes API quota and retains
synthetic test artifacts under a new `hy-pro-bench-*` directory. Never change
the implementation mid-run and treat that mixed run as a clean version comparison.

## Known operational boundary

If a process dies after Hy-Memory persists a write but before the adapter commits
its idempotency marker, a retry can repeat the Hy-Memory write. The adapter marks
interrupted requests retryable on restart and Hy-Memory's reconciliation normally
deduplicates their extracted facts. Avoid terminating the service during Add and
drain traffic before planned restarts.

## Submission materials

Use [SUBMISSION.md](SUBMISSION.md) as the disclosure and operations checklist. It
already identifies reused work and lists the adapter-specific changes that need to
be declared to AML.

## License and attribution

Adapter code in this repository is provided under Apache License 2.0. Hy-Memory is
copyright Tencent and separately distributed under Apache License 2.0. AML and its
datasets/evaluation materials remain subject to their respective licenses and rules.

