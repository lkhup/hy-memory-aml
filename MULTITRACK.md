# Three tracks on one public port

The new runtime uses the frozen text adapter as the unchanged legacy backend.
`multitrack.gateway:app` exposes port 18793 and forwards original request bytes:

| Public path | Backend | Storage |
| --- | --- | --- |
| `/add`, `/search`, `/health` (also `/textual/...`) | Legacy text, loopback 18790 | Existing text data and settings |
| `/coding/add`, `/coding/search`, `/coding/health` | New API, loopback 18788 | `data/coding` |
| `/multimodal/add`, `/multimodal/search`, `/multimodal/health` | New API, loopback 18789 | `data/multimodal` |

Internal ports are not additional public submission ports. Each new backend runs
one worker and uses a dedicated server-only authentication secret. The gateway
forwards authentication unchanged; it never repeats a POST automatically.

The legacy `app/` package is retained verbatim from commit b78edcc. Its algorithms,
provider settings and existing databases are not changed by the migration.
Moving its listener to loopback requires one graceful process restart. Existing
Full results do not become results for this new code version.

## Memory and evidence

Both new tracks complete the Hy-Memory Pro LLM pipeline before acknowledging Add.
Original code is split into ordered, lossless 4,000-character chunks; source
retrieval combines dense vectors and SQLite FTS5 ranks, supplemented by Hy-Memory
facts. Each successful Hy-Memory chunk is checkpointed so a later embedding
failure does not re-run completed chunks on ordinary retries. Process death
between SDK persistence and checkpoint commit still has the original at-least-once
write boundary; reconciliation normally deduplicates, but this is not exactly-once.

Multimodal content accepts ordered text and inline JPEG/PNG/WebP arrays. With
`VISION_ENABLED=true`, gpt-4o-mini describes visual evidence for indexing. The
original arrays, including original image bytes, remain stored and are returned
as complete source evidence. Query images are described without answering the
question. Caption-only mode is explicitly configurable; it is not the default.
The input image limits are 10 MiB per image and 30 MiB per request. Complete
records are selected within a 30 MiB image response budget. Unsupported transport
payloads over 48 MiB receive 413 rather than silent content trimming.

Hy-Memory's LLM and embedding connection pools stay alive through individual
request errors. Reconciliation degradation becomes a retryable failed write,
not a successful write that skipped the merge judgment. Original SDK extraction
and reconciliation prompts remain in use. No benchmark-specific answers, gold
data or hard-coded solutions are included.

## Concurrency and failures

Per new track, Add and Search each admit up to 64 active requests. At most 256
additional requests per operation wait asynchronously, including user write-lock
waiters. A waiting user does not consume a worker thread or a global active slot.
Same-user writes remain serialized to preserve update ordering. Queue saturation
or expiry returns 429 with Retry-After. A failed required dependency returns 503;
failed writes can be retried with the original request ID and payload.

The two new backends share one SQLite lease budget for actual provider requests:
LLM starts at 32, has a hard ceiling of 48, and embeddings have a ceiling of 64.
429/502/transport errors reduce the active target and trigger cooldown; successful
traffic gradually restores it. Requests retry with jitter and Retry-After, with
four total transport attempts and no nested SDK retry multiplication. The legacy
text service retains its original per-operation limits of 16; its SDK fan-out is
not counted in the new-services budget. The shared limit is a hard bound for the
two new backends, not for the unchanged legacy process. Raising HTTP admission
does not increase a provider's quota.

Authenticated `/coding/metrics` and `/multimodal/metrics` expose active/queued/
completed/rejected counts and observed provider concurrency, without payloads or
credentials. Server logs and synthetic load reports stay out of Git.

## Verification and operations

Run the legacy and new regression tests with pytest. The added tests cover media
validation, original-image evidence, user isolation, durable idempotency, lossless
code splitting, successful-chunk retry checkpoints, async fairness, real 64-way
backend admission, shared budgets and injected upstream failure recovery.

`scripts/stage_multitrack.py` is a designated-host setup script: it reads model
credentials locally on that server and starts only the two isolated backends.
`scripts/bench_tracks.py` uses independent synthetic users and consumes model quota.
`scripts/switch_public_gateway.py` validates preconditions and can perform the
one-time public listener migration with rollback. These are operator tools;
inspect their paths before use on another host.

`scripts/supervise_tracks.py` adopts the deployed processes and restarts a backend
or gateway after process exit, with a 30-second restart backoff. It runs as the
service owner and survives SSH disconnects. This is process crash recovery;
automatic startup after a whole-server reboot requires host startup integration.

Use each track's official Smoke before starting its Full. Synthetic load results
measure a particular provider and message size; they do not establish Full-run
quality or guarantee a sustained error-free request rate. The API remains a memory
evidence service, and platform Answer/Eval remain outside the participant runtime.
