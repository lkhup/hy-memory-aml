# Hy-Memory AML Adapter

An open-source, self-hosted adapter that exposes Tencent Hy-Memory 2.0 through the
Agent Memory Leaderboard (AML) Cycle 2 Textual Memory `Add` and `Search` contract.

The service is intentionally limited to memory ingestion and evidence retrieval.
It never generates the benchmark's final answer.

## Architecture

```text
AML evaluator
    │ HTTPS + Memory System Key
    ▼
Caddy ──► FastAPI adapter ──► Hy-Memory 2.0.3 ──► Qdrant
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
- persistent Qdrant and adapter volumes;
- automatic HTTPS through Caddy.

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

- Python 3.11–3.13
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

The Compose deployment overrides the vector-store settings to use Qdrant. For a
non-Compose local run, remove or adjust the Qdrant variables in `.env`.

## Server deployment

Recommended baseline when LLM and embedding inference use remote APIs:

- Ubuntu 22.04 or 24.04;
- 8 vCPU, 16 GiB RAM minimum (32 GiB preferred);
- 100 GiB SSD;
- Docker Engine with the Compose plugin;
- a domain whose A/AAAA record points to the server;
- inbound TCP 80 and TCP/UDP 443 allowed.

### 1. Prepare configuration

```bash
git clone <your-public-repository-url> hy-memory-aml
cd hy-memory-aml
cp .env.example .env
```

Generate two different secrets:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

Put one in `API_TOKEN` and the other in `QDRANT_API_KEY`. Set `DOMAIN`,
`ACME_EMAIL`, and the LLM/embedding credentials. Never commit `.env`.

### 2. Start the stack

```bash
docker compose pull
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs -f aml-api
```

Qdrant is reachable only on the internal Compose network. Caddy is the only public
service and obtains/renews the TLS certificate automatically.

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
- container and server restarts preserve all data;
- the service sustains the Add/Search concurrency declared in the AML request;
- `/search` returns evidence, never a final answer.

### 4. Back up persistent data

The deployment uses named volumes `hy-memory-aml_aml_data` and
`hy-memory-aml_qdrant_data`. Configure provider-level volume snapshots before the
Full run. Do not remove the volumes while an evaluation is active.

### 5. Upgrade safely

Freeze the commit and image used for the formal evaluation. Do not deploy code or
configuration changes during a Full run. Before an upgrade, snapshot both volumes,
build a new versioned image, run smoke against a staging instance, and then switch.

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
- `HY_MEMORY_READER=hybrid` lets Qdrant contribute keyword/BM25 candidates.
- `MEMORY_EMBEDDING_DIMS` must exactly match the embedding model's output.
- `RAW_EVIDENCE_LIMIT` controls how many relevant source messages may supplement
  Hy-Memory's semantic results.
- `INCLUDE_OPTIONS_IN_QUERY` defaults to `false`; enable it only after evaluating
  whether multiple-choice option text improves retrieval without adding noise.
- Run one API worker. The adapter handles bounded concurrency within that process
  and uses per-user locks so Search cannot observe a partially completed Add.

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

