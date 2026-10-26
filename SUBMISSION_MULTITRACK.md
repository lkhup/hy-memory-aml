# Additional track version 0.2.0

Textual results remain associated with the original frozen b78edcc text version.
This version adds separate Coding and Multimodal endpoints under the same public
port. Each new track needs its own official Smoke, Full and review; synthetic
tests are not formal scores.

- Coding: `/coding/add`, `/coding/search`, `/coding/health`.
- Multimodal: `/multimodal/add`, `/multimodal/search`, `/multimodal/health`.
- Add and Search admission: 64 active requests per operation per new track.
- Provider budgets: shared across the two new backends, LLM ceiling 48 and
  embedding ceiling 64; adaptive feedback can reduce actual concurrency.
- Models: gpt-4o-mini for LLM extraction, reconciliation and visual description;
  text-embedding-v4 with 1,024 dimensions for embeddings.
- Hy-Memory 2.0.3 Pro chat pipeline, including the original extraction and
  reconciliation prompts. Coding source is indexed as ordered text, not executed.
- Source evidence: complete code chunks or original ordered text/image parts;
  dense/FTS rank fusion supplemented by Hy-Memory memories. Search returns evidence,
  never the final benchmark answer.
- Additional changes: async admission, per-user write serialization, request
  journals and chunk checkpoints, shared HTTP-client retention on errors,
  bounded transport retry, strict rejection of degraded reconciliation,
  separate persistent directories, one-port gateway and process crash recovery.

Original authorship and license attribution remain in SUBMISSION.md and LICENSE.
Reference implementation: https://github.com/Tencent-Hunyuan/Hy-Memory .
No benchmark corpora, private evaluation outputs, credentials or model weights
are distributed by this repository.

The deployed Git commit must be recorded with the version request. Keep model
credentials and the Memory System Key private. Platform review determines final
eligibility; introducing this version does not revise a completed Textual score.
