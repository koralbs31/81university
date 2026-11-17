# Application simplification

Requested removals: podcasts; standalone playgrounds; library and its resource activities/blocks; calendar; boards and their collaboration service; all payment/checkout/subscription/provider features; course creator/update panel and announcement CRUD; profile bio/details/custom builder; duplicate learner course search. UI languages: English, Hebrew, Arabic.

Database backup: `.local-backups/before-feature-removal.dump` (private, ignored by Git). Earlier source changes: `.local-backups/before-feature-removal.patch`.

Applied migrations: `c93e1b7d2a60` removes feature tables and profile columns; `d04f2c8e3b71` removes purchased-pack/subscription history and orphan access metadata. Historical migrations remain as upgrade history. Removed data requires the backup to restore.

The API and web remain separate native processes. Redis and PostgreSQL remain required. `apps/collab` was dedicated to boards and has been removed.

Course contributor permissions remain because course editing authorization uses them. Ordinary lesson uploads, audio files, PDF/video lessons, assignments, and code exercises remain; they do not need the library or standalone playground features.


Built-in AI is removed: course/quiz/assignment/scenario/image/audio generation, editor and lesson assistants, magic blocks, agents and their reminders, RAG/embeddings/indexing, automatic captions and chapters, MCP/OAuth connections, provider configuration and dependencies. Static course content, uploaded media, existing subtitle playback and manual video chapter editing remain. The AI tables and agent-reference columns are dropped by migration `e15a3d9f4c82`; historical migrations remain as upgrade history.

Kubernetes now deploys only the independent web and API images. Deploy with the Helm chart in `deploy/helm/learnhouse`; see `deploy/helm/learnhouse/README.md`. No collaboration workload remains.

`f26b4e0a5d93` removes retired library activities/editor nodes from saved and historical documents, cleans demo ownership rows and removes the retired resource activity enum value. Private-course visibility is retained through the course visibility helper; role validation uses only the remaining permission buckets.
