# [1.56.0](https://github.com/cboulanger/zotero-rag/compare/v1.55.1...v1.56.0) (2026-10-08)


### Bug Fixes

* **backend,plugin:** close duplicate-run race and show readable rate-limit message ([06af946](https://github.com/cboulanger/zotero-rag/commit/06af94656b6b186a407015558ec598ec9d17d5e3))
* **plugin:** keep per-library Index button disabled until its run truly ends ([e27b5ee](https://github.com/cboulanger/zotero-rag/commit/e27b5ee5b471c4dbdeb4934d91660a51ecfb561d))


### Features

* dynamic remote preset config with runtime preset switching ([a2288d9](https://github.com/cboulanger/zotero-rag/commit/a2288d9256d4109debf2d40e5d82bb087b107a51))

## [1.55.1](https://github.com/cboulanger/zotero-rag/compare/v1.55.0...v1.55.1) (2026-10-08)


### Bug Fixes

* **deploy:** stop Kreuzberg-restart fix from crash-looping the main service ([fdb7f94](https://github.com/cboulanger/zotero-rag/commit/fdb7f94d47b814fbc7bd4288a9368690381ad977))

# [1.55.0](https://github.com/cboulanger/zotero-rag/compare/v1.54.0...v1.55.0) (2026-10-08)


### Bug Fixes

* **backend,plugin:** persist stopped-run state and distinguish abort from crash ([a2d1e92](https://github.com/cboulanger/zotero-rag/commit/a2d1e929a385fed9567a095c2692b7f15b21fe8b))
* **backend:** index attachment_title to prevent purge-snapshots timeout ([21002ba](https://github.com/cboulanger/zotero-rag/commit/21002ba72882203d95f84e607d67aae14d4cdd6c))
* **backend:** stop recycled PIDs from wedging autoindex status and crashing abort ([81632f1](https://github.com/cboulanger/zotero-rag/commit/81632f10c91d29062827d4244540af4ee93bf861))
* **deploy:** restart Kreuzberg sidecar on main container restart ([1edf366](https://github.com/cboulanger/zotero-rag/commit/1edf3666fe42e9d633f9d15f51f90b9a6552e2a9))
* **plugin:** disable per-library Skip button when no run is active ([ee99b84](https://github.com/cboulanger/zotero-rag/commit/ee99b84f37382df17f2d2fb852fdd384e8774ce9))


### Features

* **backend,plugin:** admin system health panel; move run-banner to bottom ([fb8a03a](https://github.com/cboulanger/zotero-rag/commit/fb8a03a2932632454d480dfbabb94ddb62399bcf))
* **backend,plugin:** per-library run-now and align status badge layout ([8ba17b8](https://github.com/cboulanger/zotero-rag/commit/8ba17b8dbf7eb5ebbf49e1627287b2102823b84f))
* **plugin,backend:** reorganize status dialog sections; next-run countdown ([13efbbd](https://github.com/cboulanger/zotero-rag/commit/13efbbdb96ca15d050f0a1e8c8b74a81891d74e5))

# [1.54.0](https://github.com/cboulanger/zotero-rag/compare/v1.53.0...v1.54.0) (2026-10-08)


### Features

* **plugin:** show progress while checking queued status in Fix Unavailable ([f3bfe97](https://github.com/cboulanger/zotero-rag/commit/f3bfe9750021050226f3761c7b37d179129408be))

# [1.53.0](https://github.com/cboulanger/zotero-rag/compare/v1.52.2...v1.53.0) (2026-10-07)


### Bug Fixes

* **backend:** report progress while draining pending uploads ([12e2264](https://github.com/cboulanger/zotero-rag/commit/12e2264ed301066cfd27a5a3d5356057f26e227a))
* **backend:** stop draining pending uploads once the embedding quota is exhausted ([2a4e44f](https://github.com/cboulanger/zotero-rag/commit/2a4e44febfd534a2175bdafceb555c2ecaffe0ca))
* **plugin:** check "Include missing attachments" by default in Fix Unavailable ([20b8280](https://github.com/cboulanger/zotero-rag/commit/20b82800d085a05adf3a89d58af29ca3f159b13a))
* **plugin:** exclude bare web-link attachments from Fix Unavailable list ([790573f](https://github.com/cboulanger/zotero-rag/commit/790573f2dc5a3dd5d291ec22ca00fdb37b2dc4b4))


### Features

* **backend:** refuse attachments over a hard size cap instead of OOM-killing kreuzberg ([2a43323](https://github.com/cboulanger/zotero-rag/commit/2a43323f082b3ba794930e22be2ee4b7e236ba22))
* **migration:** add patient retry tier for long connectivity outages ([4c2d576](https://github.com/cboulanger/zotero-rag/commit/4c2d576ae14d15c4e0cd3bdfebe4db409c42f342))
* **plugin:** add "Copy Row Data Only" option to Fix Unavailable dialog ([8e62da5](https://github.com/cboulanger/zotero-rag/commit/8e62da5531050cf52c7dd0616ac9bda00ad1e3f9))
* **plugin:** surface server-refused "too large" attachments in Fix Unavailable ([51054b3](https://github.com/cboulanger/zotero-rag/commit/51054b36e686fa2b1ae34592f30067d8fee6df10))

## [1.52.2](https://github.com/cboulanger/zotero-rag/compare/v1.52.1...v1.52.2) (2026-10-06)


### Bug Fixes

* **backend:** use live item/attachment version when draining pending uploads ([e3f7f34](https://github.com/cboulanger/zotero-rag/commit/e3f7f345c7798793af2b4eced54a99f0492b0fc1))

## [1.52.1](https://github.com/cboulanger/zotero-rag/compare/v1.52.0...v1.52.1) (2026-10-06)


### Bug Fixes

* **backend:** size indexing chunks for the active embedding model, not generic defaults ([64545bb](https://github.com/cboulanger/zotero-rag/commit/64545bbd567a9843f9a9c77dabb93cac519209fa))
* **backend:** use the live item version, not the stale deferral-time one, on process-now ([cea9713](https://github.com/cboulanger/zotero-rag/commit/cea971387f5b6abbbf7f876df96eeb9ad5f683b3))
* **plugin:** fix dropdown button cropping, add progress meter and cancel/resume to Fix Unavailable ([5c6bb5d](https://github.com/cboulanger/zotero-rag/commit/5c6bb5d8bcbc291418cfa93597e7bbdc96f25ad3))

# [1.52.0](https://github.com/cboulanger/zotero-rag/compare/v1.51.0...v1.52.0) (2026-10-06)


### Bug Fixes

* **backend:** guard compute_queue_eta against a naive cron_status.json timestamp ([d950a5b](https://github.com/cboulanger/zotero-rag/commit/d950a5b818d0cebe99a4365e0481f9159dd80641))
* **backend:** isolate malformed pending-upload cache entries in _drain_pending_uploads ([12416b8](https://github.com/cboulanger/zotero-rag/commit/12416b8002b6d054a3beaa2c3d3c0cf8274e4de8))
* **backend:** run pending_upload_cache file I/O off the event loop ([f2dff53](https://github.com/cboulanger/zotero-rag/commit/f2dff53305c9fa67bd49c31ac5b7dafee06b835e))
* **embeddings:** raise instead of returning None when truncation is exhausted ([ba6471e](https://github.com/cboulanger/zotero-rag/commit/ba6471e1be3598bb70644d85e2923894dab6ac74))
* **migration:** retry all httpx transport errors, not just connect/timeout ([15b0352](https://github.com/cboulanger/zotero-rag/commit/15b0352acfe9ce14d904517b90cabbcebafecf7e))
* **plugin:** correctly flag link-only attachments in download-failed backlog ([5cb3eaa](https://github.com/cboulanger/zotero-rag/commit/5cb3eaac98c9ee3031df22b55dc47b0da6e68252))
* **plugin:** fix debug-report gap for already-queued rows in Fix Unavailable dialog ([d74a3ed](https://github.com/cboulanger/zotero-rag/commit/d74a3ed6bc021ff87ef1264ff326e4d3ac5f81ea))
* **plugin:** make getQueuedStatusMap fail gracefully, add check_failed test ([08691f3](https://github.com/cboulanger/zotero-rag/commit/08691f3fcab1d7b8d47c87e877fbc1d22b1f7e6f))
* **plugin:** read queueBlockReason instead of nonexistent result.reason ([0dff410](https://github.com/cboulanger/zotero-rag/commit/0dff410a1367d864f8adef4a51ae59a6d0742e72))
* **plugin:** remove dead 404 branch in _processQueuedNow, add logging ([0d79aba](https://github.com/cboulanger/zotero-rag/commit/0d79aba3674722611383ce4f26da94ec3e3e3369))


### Features

* **backend:** add pending-upload cache storage primitives ([2f9b7b3](https://github.com/cboulanger/zotero-rag/commit/2f9b7b348cc583b72acdbef1167360a04e18e5ce))
* **backend:** add POST /api/index/document/cache deferred-upload endpoint ([216f59a](https://github.com/cboulanger/zotero-rag/commit/216f59aac656e6a30b5af409ec45d433740522ec))
* **backend:** add process-now endpoint to force-index a cached upload ([cc8e826](https://github.com/cboulanger/zotero-rag/commit/cc8e8265f69b1bd91c53c9f66e3169d665d53e40))
* **backend:** compute deferred-indexing ETA and block reason ([e86e2b3](https://github.com/cboulanger/zotero-rag/commit/e86e2b39da7906f576ef9d09a7002c25fbdd79c9))
* **backend:** drain each library's pending-upload cache during its autoindex run ([08a4fc0](https://github.com/cboulanger/zotero-rag/commit/08a4fc02cb578af66da0ea78f1c1244666f16d87))
* **backend:** report queued/eta status from check-indexed for cached uploads ([5538e4d](https://github.com/cboulanger/zotero-rag/commit/5538e4d69f75b03470d170dbda982d9302596185))
* **migration:** add --mode flag and interactive resume/clean prompt ([5bcb9a6](https://github.com/cboulanger/zotero-rag/commit/5bcb9a62941bbd597effb0f7002ed630e912f4ef))
* **migration:** add resume-from-cursor state persistence module ([56fcc3b](https://github.com/cboulanger/zotero-rag/commit/56fcc3be1d20d44278d02ef02475ed1f957bd515))
* **migration:** make run_migration mode/state-aware for resume support ([f34ce3e](https://github.com/cboulanger/zotero-rag/commit/f34ce3ead47c267c3420e701d41b1303499aba9c))
* **plugin:** add defer mode to RemoteIndexer._uploadAttachment ([4ade264](https://github.com/cboulanger/zotero-rag/commit/4ade2644bf5b8fb007c42b5ed9bca59a78f29db2))
* **plugin:** add getQueuedStatusMap for the Fix dialog's queued-row display ([74a3cbc](https://github.com/cboulanger/zotero-rag/commit/74a3cbc94fd7e4d1d6c37fafbc02347946cb3640))
* **plugin:** add RemoteIndexer._processQueuedNow for forcing a cached upload ([c2f78d4](https://github.com/cboulanger/zotero-rag/commit/c2f78d448c543c85c3e4fe9fd2cfd7d07af1dfa1))
* **plugin:** add split-button markup and queued-status styling to Fix Unavailable dialog ([b00c72f](https://github.com/cboulanger/zotero-rag/commit/b00c72f067eff6d261500eebbf991ad9d343f3bf))
* **plugin:** make server-download-failed attachments opt-in, with concrete status reasons ([cd5aa7b](https://github.com/cboulanger/zotero-rag/commit/cd5aa7b327252e4c75ea35992978a397d9d59b8d))
* **plugin:** thread defer mode through Fix Unavailable upload wrappers ([b4a9127](https://github.com/cboulanger/zotero-rag/commit/b4a91277b0ee00eb191af1b02d0c5a6ed8dd96af))
* **plugin:** wire split-button and queued-row handling into Fix Unavailable dialog ([9fd631d](https://github.com/cboulanger/zotero-rag/commit/9fd631deb91ea9e4d9e4687f75f2dafba78cc17c))

# [1.51.0](https://github.com/cboulanger/zotero-rag/compare/v1.50.0...v1.51.0) (2026-10-05)


### Bug Fixes

* **autoindex:** skip indexing runs when disk space is critically low ([f9569f8](https://github.com/cboulanger/zotero-rag/commit/f9569f83cb6babf01a9840bea29c5ba59d9579c7))
* **ci:** call release.yml as a reusable workflow instead of via workflow_run ([7032a45](https://github.com/cboulanger/zotero-rag/commit/7032a45438ee32ad8e8a4459c4ded8fa4e19e8d3))
* **ci:** override GITHUB_REF/GITHUB_REF_NAME for semantic-release's branch detection ([d067930](https://github.com/cboulanger/zotero-rag/commit/d067930ffdc026fc0485efa54986edf1388e5b39))
* **ci:** pin release workflow checkouts to main, not the default branch ([5dcc37e](https://github.com/cboulanger/zotero-rag/commit/5dcc37e3f7d7eeb4877f7afb1f534de1a83ed072)), closes [#46](https://github.com/cboulanger/zotero-rag/issues/46)
* **ci:** use a scoped admin PAT for semantic-release's push to protected main ([1f1c03a](https://github.com/cboulanger/zotero-rag/commit/1f1c03afd6030fc712905efa8ca00511855eec64))
* **deploy:** prune dangling images before pulling new ones ([5b8183b](https://github.com/cboulanger/zotero-rag/commit/5b8183b697cae3e1c89d1ee1a4c6520408f4ae02))
* **migration:** clean up migrate_library.py help text and error handling ([25b5db6](https://github.com/cboulanger/zotero-rag/commit/25b5db6bda82d5e91ea851f43f35a306f18c836e))
* **migration:** reject malformed import payloads with 400 instead of 500 ([833c0be](https://github.com/cboulanger/zotero-rag/commit/833c0be77b469428b4719f45121aa3fc1fa91446))
* **migration:** retry on transient 5xx responses and add transfer progress reporting ([a789650](https://github.com/cboulanger/zotero-rag/commit/a789650950206666c126a134015ad8a5f2ce9ed5))
* **quality-review:** don't suppress pre-generation escalation for the flag ([fe86819](https://github.com/cboulanger/zotero-rag/commit/fe86819d895cbc63bd1d5106d8b0119e44ea0b5f))
* **quality-review:** remove stale no-op caveat, pin the escalation-budget regression ([3ea3db4](https://github.com/cboulanger/zotero-rag/commit/3ea3db4d25fffc761e7821ca29241210c9f0f23b))
* **quality-review:** replace hedge-phrase regex with explicit sentinel ([a5c213f](https://github.com/cboulanger/zotero-rag/commit/a5c213f6bcceb4030b39fbafd940568fb53afe5b))
* **query:** surface the real Qdrant error instead of a generic message ([3306f31](https://github.com/cboulanger/zotero-rag/commit/3306f315ce2147d29501b49fd30d744cec49919b))
* **rag-engine:** dedup identical-text chunks before per-document truncation ([a6ec447](https://github.com/cboulanger/zotero-rag/commit/a6ec44797fe88bb0226cc203e76626b608f37840))
* **retrieval:** group diversity by item_key, not attachment_key ([81c10c4](https://github.com/cboulanger/zotero-rag/commit/81c10c4f06a38efe961a1af21f768c02557b7368))
* **router:** drop author/title/citation filters not mentioned in the question ([80e6427](https://github.com/cboulanger/zotero-rag/commit/80e64271bef63d84d75b74429785845829835dde))
* **router:** instruct the routing LLM not to infer authors from topic ([17b8929](https://github.com/cboulanger/zotero-rag/commit/17b8929d2d6f02af4169b0f8a1119da87cb6c35f))


### Features

* **autoindex:** reserve CPUs for RAG queries during indexing runs ([d8277b9](https://github.com/cboulanger/zotero-rag/commit/d8277b9832328fd254205ff2f3d6376e7c293c5d))
* **migration:** add admin-gated /api/migration/* endpoints ([b3e7152](https://github.com/cboulanger/zotero-rag/commit/b3e715240382e721f4400a911064d600e4aad7ac))
* **migration:** add bin/migrate_library.py CLI ([8c28ae2](https://github.com/cboulanger/zotero-rag/commit/8c28ae29cae3d479180cf6162bc228e47d851e14))
* **ops:** add production health monitoring with ntfy.sh alerts ([0f6ead1](https://github.com/cboulanger/zotero-rag/commit/0f6ead11ab8bd6805858cdc66392a84d6767d574))
* **quality-review:** add _thin_context_coverage detector ([29f9d2d](https://github.com/cboulanger/zotero-rag/commit/29f9d2dac01fb2bdb995ad810fd3b9a3a2f301ac))
* **quality-review:** escalate retrieval once on thin-context answers ([0ec7a4c](https://github.com/cboulanger/zotero-rag/commit/0ec7a4ccf80ded3ed58bf7e0937f828033552499))
* **quality-review:** thread enable_quality_self_review flag end-to-end ([8e4dbb5](https://github.com/cboulanger/zotero-rag/commit/8e4dbb5f92d9ed1dcee95a0b020f0e87f6e918ee))
* **rag-engine:** instruct the model to answer in the question's language ([85eb4ad](https://github.com/cboulanger/zotero-rag/commit/85eb4ad9dc956d3dacef92402f8bd5a89d0204c0))
* **router:** add dropped_filters field to QueryPlan ([2b84fbb](https://github.com/cboulanger/zotero-rag/commit/2b84fbb23976c2f3a925d0d817fce10ba810e6ec))
* **vector-store:** add export/import/count methods for cross-instance library migration ([ff20b2a](https://github.com/cboulanger/zotero-rag/commit/ff20b2a3ade90cee8de805b01043137e9913a8d0))

# [1.50.0](https://github.com/cboulanger/zotero-rag/compare/v1.49.2...v1.50.0) (2026-10-03)


### Features

* **fix-unavailable:** add optional debugging-information download ([#45](https://github.com/cboulanger/zotero-rag/issues/45)) ([83c2526](https://github.com/cboulanger/zotero-rag/commit/83c2526aba061d7df5c95860266b3c650cb97c1e))

## [1.49.2](https://github.com/cboulanger/zotero-rag/compare/v1.49.1...v1.49.2) (2026-10-02)


### Bug Fixes

* **fix-unavailable:** retry "no text" attachments instead of giving up immediately ([e43ae30](https://github.com/cboulanger/zotero-rag/commit/e43ae301cbcb5c7a16527eea4030580be7d1f7cb))

## [1.49.1](https://github.com/cboulanger/zotero-rag/compare/v1.49.0...v1.49.1) (2026-10-02)


### Bug Fixes

* **fix-unavailable:** stop the failed-download cap from silently hiding new failures ([7190821](https://github.com/cboulanger/zotero-rag/commit/71908212aeee03e8f691263e7f86029c524977d0))

# [1.49.0](https://github.com/cboulanger/zotero-rag/compare/v1.48.1...v1.49.0) (2026-10-02)


### Bug Fixes

* **api:** clamp timeout_multiplier to [1.0, 10.0] ([3c9d5c9](https://github.com/cboulanger/zotero-rag/commit/3c9d5c9a93e69ff6a2ffac735e227646682a4dab))
* **extraction:** classify fully-timed-out split PDFs as skipped_timeout, not skipped_empty ([7b8d406](https://github.com/cboulanger/zotero-rag/commit/7b8d4069bc854223c742c1cdf4c8ed1eabf1f642))
* **fix-unavailable:** update dialog header to describe both unavailability causes ([03fc8bd](https://github.com/cboulanger/zotero-rag/commit/03fc8bd7e81835f89cc3452cd8721afb13667758))
* **plugin:** add Zotero 10/11 compat shim for collection-pane selection getters ([3044d16](https://github.com/cboulanger/zotero-rag/commit/3044d16d638d4df542311ae9fe219610c0555f25))
* **plugin:** add Zotero 10/11 compat shim for fulltextWord search condition ([278ee4a](https://github.com/cboulanger/zotero-rag/commit/278ee4a9a38538276af7b0cc3c85098e6751b33c))
* **plugin:** harden fulltextContent detection against SearchConditions.get() throwing ([55526f8](https://github.com/cboulanger/zotero-rag/commit/55526f8f35519c7da68857973bf4dfe237d73e20))
* **plugin:** import FormData/Blob/AbortController into the bootstrap global ([4cf2fff](https://github.com/cboulanger/zotero-rag/commit/4cf2fffe87d65b1cdf497adbf9ba2d04d4191469))
* **plugin:** load remote_indexer.js at plugin startup, not just per-dialog ([6b5f2c5](https://github.com/cboulanger/zotero-rag/commit/6b5f2c58cc3a6407ce8a02d0029c3855809d7df8))
* **plugin:** make _apiFetch's timeout signal portable to non-window globals ([0af770b](https://github.com/cboulanger/zotero-rag/commit/0af770b11506b7951e880b30591be27b0344a16c))
* **plugin:** raise strict_max_version to 10.* for Zotero 10 compatibility ([639faf4](https://github.com/cboulanger/zotero-rag/commit/639faf4b629741650bfe96580b738a71734f13c2))
* **reindex:** reject abstract-only fallback as a false improvement ([08e0fe8](https://github.com/cboulanger/zotero-rag/commit/08e0fe8d931a16503d977336054b24b775f57d26))


### Features

* **api:** accept an optional timeout_multiplier on the document upload endpoints ([3d4f26b](https://github.com/cboulanger/zotero-rag/commit/3d4f26baf5e2fb26540df905cdcf5a54179ee9b9))
* **extraction:** make Kreuzberg timeout cap configurable via KREUZBERG_TIMEOUT_SECONDS ([35adcee](https://github.com/cboulanger/zotero-rag/commit/35adcee708bd0768578b2f48931bca726d913eb7))
* **extraction:** thread a per-call timeout_multiplier through DocumentProcessor ([0985c5c](https://github.com/cboulanger/zotero-rag/commit/0985c5c256a8a25f131875583b2a13733b7434a7))
* **fix-unavailable:** retry timeout rows with a longer timeout by default on Search & Fix ([1cfa1c8](https://github.com/cboulanger/zotero-rag/commit/1cfa1c89dc2f2be76145c803365b4410aa213e9d))
* **plugin:** add removeSkippedServerItems and retryTimeoutSkippedAttachment ([9946de9](https://github.com/cboulanger/zotero-rag/commit/9946de9afc59c5867b3bd8e718b92d9244705b28))
* **plugin:** let _uploadAttachment request a scaled-up extraction timeout ([6e64fe9](https://github.com/cboulanger/zotero-rag/commit/6e64fe93af91a5c2c98642ba3bb4ea128fc10b1b))

## [1.48.1](https://github.com/cboulanger/zotero-rag/compare/v1.48.0...v1.48.1) (2026-10-02)


### Bug Fixes

* **fix-unavailable:** restore checkbox/status/select cells on newer Zotero ([6d8c54d](https://github.com/cboulanger/zotero-rag/commit/6d8c54d9f3eeb0f5c1ff7277a2fcd4fc77b99f8b))

# [1.48.0](https://github.com/cboulanger/zotero-rag/compare/v1.47.0...v1.48.0) (2026-10-02)


### Bug Fixes

* **fix-unavailable:** prune fixed entries from the download-failed store ([7c1fa2f](https://github.com/cboulanger/zotero-rag/commit/7c1fa2f1e92c3229ca986a1239edf54a10ba8917))
* **reindex:** handle embedding quota exhaustion in --loop instead of crashing ([3ed9541](https://github.com/cboulanger/zotero-rag/commit/3ed954103aaac855670195a076722f702843cebd))


### Features

* **sync:** surface incremental sync's download failures to Fix Unavailable ([3c6aeec](https://github.com/cboulanger/zotero-rag/commit/3c6aeecc263c351d36d3af41629e33204eca5abd))

# [1.47.0](https://github.com/cboulanger/zotero-rag/compare/v1.46.0...v1.47.0) (2026-10-02)


### Bug Fixes

* **reindex:** exclude items whose reprocessing produced no reduction ([281a95d](https://github.com/cboulanger/zotero-rag/commit/281a95d950522552daa911480197474d2126aaf6))


### Features

* **reindex:** surface download failures to the Fix Unavailable UI ([2644f44](https://github.com/cboulanger/zotero-rag/commit/2644f4464e03ffeaff87477d05b69e29d84c051c))

# [1.46.0](https://github.com/cboulanger/zotero-rag/compare/v1.45.2...v1.46.0) (2026-10-01)


### Features

* **reindex:** add --loop mode for unattended batch reprocessing ([1e80a59](https://github.com/cboulanger/zotero-rag/commit/1e80a593589e4fce0b0c25d9b635ec4f3634da9e))

## [1.45.2](https://github.com/cboulanger/zotero-rag/compare/v1.45.1...v1.45.2) (2026-10-01)


### Bug Fixes

* **reindex:** never delete chunks before confirming replacement content exists ([38d1a7b](https://github.com/cboulanger/zotero-rag/commit/38d1a7b92dcd04431fe10024c28a5dd288441c40))

## [1.45.1](https://github.com/cboulanger/zotero-rag/compare/v1.45.0...v1.45.1) (2026-10-01)


### Bug Fixes

* **embeddings:** retry all InternalServerError responses, not just "try again" ([bc931b5](https://github.com/cboulanger/zotero-rag/commit/bc931b507cd3d389e62ad1ce32ad216f4f52aae2))

# [1.45.0](https://github.com/cboulanger/zotero-rag/compare/v1.44.3...v1.45.0) (2026-10-01)


### Features

* **deploy:** forward extra args to container.mjs deploy ([764e241](https://github.com/cboulanger/zotero-rag/commit/764e24169f53416bf81d007f4e7c783c7b361e05))

## [1.44.3](https://github.com/cboulanger/zotero-rag/compare/v1.44.2...v1.44.3) (2026-10-01)


### Bug Fixes

* **document-processor:** isolate per-attachment failures within an item ([913b9a1](https://github.com/cboulanger/zotero-rag/commit/913b9a16bb23c08cec32e4e92c6ca7bcf69bd8ab))

## [1.44.2](https://github.com/cboulanger/zotero-rag/compare/v1.44.1...v1.44.2) (2026-10-01)


### Bug Fixes

* **document-processor:** add force_extraction to bypass cross-library dedup ([d6b6325](https://github.com/cboulanger/zotero-rag/commit/d6b6325189cfc59bc6f7dc19d914fa1d2f7e1206))

## [1.44.1](https://github.com/cboulanger/zotero-rag/compare/v1.44.0...v1.44.1) (2026-10-01)


### Bug Fixes

* move container-dependent admin scripts from scripts/ to bin/ ([393da9c](https://github.com/cboulanger/zotero-rag/commit/393da9c27d28d40321facdc887d9263cd00be7c8))

# [1.44.0](https://github.com/cboulanger/zotero-rag/compare/v1.43.0...v1.44.0) (2026-10-01)


### Features

* **scripts:** add targeted reindex for oversized already-indexed items ([146acb7](https://github.com/cboulanger/zotero-rag/commit/146acb7cda6db19e78e26ea5d890e44db2d20812))

# [1.43.0](https://github.com/cboulanger/zotero-rag/compare/v1.42.1...v1.43.0) (2026-10-01)


### Bug Fixes

* **chunking:** merge tiny per-page extractor chunks before storage ([e8d9f9f](https://github.com/cboulanger/zotero-rag/commit/e8d9f9f35afea96726831d81d1760a4cf600e3ec))
* **llm:** disable KISSKI thinking mode and guard against empty completions ([eb72356](https://github.com/cboulanger/zotero-rag/commit/eb72356f3080b1aa7b2cb947c74bf0c05754bbae))
* **query:** handle Qdrant search timeouts gracefully instead of 500s ([f9f89c1](https://github.com/cboulanger/zotero-rag/commit/f9f89c1ad7af18f8b49dd05d106ea7cd9f7870ab))


### Features

* **vector-store:** enable int8 scalar quantization on document_chunks ([1416caa](https://github.com/cboulanger/zotero-rag/commit/1416caa690319d7e3726d1e2a3dda650de3409e2))

## [1.42.1](https://github.com/cboulanger/zotero-rag/compare/v1.42.0...v1.42.1) (2026-07-24)


### Bug Fixes

* **citations:** resolve [SN:P] citations left unlinked by a literal "P" placeholder ([8119d5c](https://github.com/cboulanger/zotero-rag/commit/8119d5c404452798034beda14c52e68ff07fb80c))
* **dialog:** open citation links in-process instead of round-tripping through the OS ([d968c14](https://github.com/cboulanger/zotero-rag/commit/d968c140f853fdd5254d5ac97cade699e2ff0e53))
* **dialog:** resolve follow-up citation links, auto-scroll, and add loading indicator ([916302c](https://github.com/cboulanger/zotero-rag/commit/916302ce836058dec8a2c9b14333c4ede56ed04b))

# [1.42.0](https://github.com/cboulanger/zotero-rag/compare/v1.41.0...v1.42.0) (2026-07-24)


### Features

* **dialog:** add follow-up chat, citation-quality guards, and retrieval tuning ([ae72867](https://github.com/cboulanger/zotero-rag/commit/ae728673b6d2c84e61a0dd7194e3fe4e0cb79bf2))

# [1.41.0](https://github.com/cboulanger/zotero-rag/compare/v1.40.0...v1.41.0) (2026-07-21)


### Features

* answer citation questions by searching the local Zotero full-text index ([43adcde](https://github.com/cboulanger/zotero-rag/commit/43adcde258ac366683b8ed8aa77c32decf61e1c0))

# [1.40.0](https://github.com/cboulanger/zotero-rag/compare/v1.39.0...v1.40.0) (2026-07-19)


### Bug Fixes

* **backend:** paginate Qdrant scroll in update_item_metadata/get_item_chunks ([f3258ce](https://github.com/cboulanger/zotero-rag/commit/f3258ceba2b368aed552938c8614675738c525a7))
* **plugin:** isolate per-item failures in the modify-notifier loop ([c76bccb](https://github.com/cboulanger/zotero-rag/commit/c76bccbdcc55f03d68211780c6dc24ad8690dbfa))
* **plugin:** map libraryID to backend library_id in delete-notifier ([83b7571](https://github.com/cboulanger/zotero-rag/commit/83b7571ccba8b62485e49e6aa06efc4a0596b4f1))
* **plugin:** preserve partial-batch success when one library's metadata push fails ([73b8194](https://github.com/cboulanger/zotero-rag/commit/73b8194913e1c3cb76ee8f548299d582aa038f9a))
* **plugin:** prevent TaskQueue from dropping a newer edit during in-flight dispatch ([2c57932](https://github.com/cboulanger/zotero-rag/commit/2c5793245561cc096eecc3f2a24354d8bf07ed5f))
* show full text via tooltip on truncated Fix Unavailable columns ([a64295e](https://github.com/cboulanger/zotero-rag/commit/a64295e6f4d0bda9b74340add9d1874884ba2a1e))
* show full text via tooltip on truncated Fix Unavailable columns ([716e8d4](https://github.com/cboulanger/zotero-rag/commit/716e8d40997d8fa694f6d51f25eeef9aeade33a0))


### Features

* **backend:** accept tags/item_version/zotero_modified in metadata update endpoint ([eeb884d](https://github.com/cboulanger/zotero-rag/commit/eeb884dda982d04fbb347f44c3e45b2ed97f0c29))
* **plugin:** add generic debounced/batched/retrying TaskQueue engine ([0b6084d](https://github.com/cboulanger/zotero-rag/commit/0b6084d4517f889be04e05f2af4ae898edf360eb))
* **plugin:** enqueue metadata updates on item modify events ([656cfed](https://github.com/cboulanger/zotero-rag/commit/656cfeddf96a6c24c0116cc553ce4dd623aa4206))
* **plugin:** load task_queue.js at plugin startup ([a0c8a92](https://github.com/cboulanger/zotero-rag/commit/a0c8a924358aec097d81d21bae4115ba303c5846))
* **plugin:** wire TaskQueue into the plugin lifecycle for live metadata sync ([bc2f03d](https://github.com/cboulanger/zotero-rag/commit/bc2f03d3e48bc97e4ee64005294c20e9e17e7cdb))

# [1.39.0](https://github.com/cboulanger/zotero-rag/compare/v1.38.3...v1.39.0) (2026-07-19)


### Bug Fixes

* detect abstract-to-attachment transition in metadata-only fast path ([091318f](https://github.com/cboulanger/zotero-rag/commit/091318f8221bf92bcb777ca97d017cd5545a9bbe))
* index catalog-only items with no attachment/abstract as stub records ([4c83937](https://github.com/cboulanger/zotero-rag/commit/4c839377a0798313f352a1ff034ceb10bfaa8bbc))


### Features

* add _try_metadata_only_update decision logic (not yet wired in) ([41af1a0](https://github.com/cboulanger/zotero-rag/commit/41af1a0bf1851c296e173d9866d19a72254734e5))
* add update_item_bibliographic_metadata for cheap metadata patches ([f277a94](https://github.com/cboulanger/zotero-rag/commit/f277a943a6c08b63bb0e19f53d6486ae8dd71fc5))
* aggregate download failures across subprocess batches ([97c7e53](https://github.com/cboulanger/zotero-rag/commit/97c7e53bb7a95a8db210d4cd785704e4a5fac3a5))
* fetch and merge server-reported download failures on every library metadata poll ([bf8d8ad](https://github.com/cboulanger/zotero-rag/commit/bf8d8ad51a70d06f7ecfbeec5e4656661d4ef6a1))
* index Zotero tags/keywords as a filterable field ([cf446a5](https://github.com/cboulanger/zotero-rag/commit/cf446a59e379f9e79cc005d96df40bf9a2140c9a))
* label server-download-failure rows distinctly in Fix Unavailable table ([d8d2070](https://github.com/cboulanger/zotero-rag/commit/d8d207003da6503d28dd93a6a0f46091924da9c3))
* persist and merge server-reported download failures into Fix Unavailable candidates ([dd924db](https://github.com/cboulanger/zotero-rag/commit/dd924db5ea2bd54daa4789ab07db75c4ee90d68b))
* persist download-failure records on LibraryIndexMetadata (full sync inline path) ([ea35a9c](https://github.com/cboulanger/zotero-rag/commit/ea35a9c2c6bd91066179a6aa52c320572376025b))
* record per-attachment download failures on DocumentProcessor ([3963368](https://github.com/cboulanger/zotero-rag/commit/396336865cae2d51ba6991209ef529f5e4786609))
* skip reindex for metadata-only changes in full sync inline path ([1ef58ed](https://github.com/cboulanger/zotero-rag/commit/1ef58edc2c8bd66a36222c2efb98aef6306c1a90))
* skip reindex for metadata-only changes in incremental sync ([9b5c94b](https://github.com/cboulanger/zotero-rag/commit/9b5c94bfb09e8b2053644ed4e94411bff659b9a5))
* skip reindex for metadata-only changes in subprocess batch worker ([e414565](https://github.com/cboulanger/zotero-rag/commit/e414565a3187541f8ee258472d26a6eb4cad1767))

## [1.38.3](https://github.com/cboulanger/zotero-rag/compare/v1.38.2...v1.38.3) (2026-07-17)


### Bug Fixes

* surface items_failed to the plugin so it stops showing false "(incomplete)" ([7a08711](https://github.com/cboulanger/zotero-rag/commit/7a08711b7d3a9da2dc854d0d8d7588c4d199cd4d))

## [1.38.2](https://github.com/cboulanger/zotero-rag/compare/v1.38.1...v1.38.2) (2026-07-17)


### Bug Fixes

* count zero-chunk indexing results as items_failed, not items_added ([7097ae7](https://github.com/cboulanger/zotero-rag/commit/7097ae757f8b54f99b7ca29a31895cc0b4a6be2c))
* don't misclassify a same-item duplicate skip as items_failed ([87bb1f3](https://github.com/cboulanger/zotero-rag/commit/87bb1f39b6450ebdba2c8901fbf096e89d1c05ef))
* exclude Zotero Trash from local indexable-item searches ([2aca674](https://github.com/cboulanger/zotero-rag/commit/2aca6743f268a93eb848faed19d0cfbb37dfc7ba))
* index standalone Zotero attachments with no parent item ([0838e8a](https://github.com/cboulanger/zotero-rag/commit/0838e8a2ee2cd732ff09063c8026c3004f2df618))
* propagate per-user API keys to indexing subprocesses, fix stalled progress UI ([29eb5e0](https://github.com/cboulanger/zotero-rag/commit/29eb5e09783493f5fb36f29f2347019ba11d6533))
* surface silently-failing indexing items as items_failed ([251c62a](https://github.com/cboulanger/zotero-rag/commit/251c62abf31df18eb8c577280190c6e86fe896f1))

## [1.38.1](https://github.com/cboulanger/zotero-rag/compare/v1.38.0...v1.38.1) (2026-07-08)


### Bug Fixes

* label auto-indexed group libraries with real names, not raw slugs ([4c5b6a1](https://github.com/cboulanger/zotero-rag/commit/4c5b6a17f3c86e4ebe8484030ba8f02b770b5301))

# [1.38.0](https://github.com/cboulanger/zotero-rag/compare/v1.37.0...v1.38.0) (2026-07-08)


### Features

* Add in-process auto-index scheduler with admin controls ([#41](https://github.com/cboulanger/zotero-rag/issues/41)) ([e57cfd3](https://github.com/cboulanger/zotero-rag/commit/e57cfd3a3c87ddb1fc8273408b585f7d2a540ae8))

# [1.37.0](https://github.com/cboulanger/zotero-rag/compare/v1.36.2...v1.37.0) (2026-07-07)


### Features

* trigger release for auto-indexing per-user embedding keys and on-demand runs ([9a76efa](https://github.com/cboulanger/zotero-rag/commit/9a76efa2b3c60567c6f64ccb008a8f713702d605)), closes [#40](https://github.com/cboulanger/zotero-rag/issues/40)

## [1.36.2](https://github.com/cboulanger/zotero-rag/compare/v1.36.1...v1.36.2) (2026-07-07)


### Bug Fixes

* index items sharing duplicate attachment content instead of skipping them ([1f91d7c](https://github.com/cboulanger/zotero-rag/commit/1f91d7c254ef668747929bc729c9b6e5220b2707))

## [1.36.1](https://github.com/cboulanger/zotero-rag/compare/v1.36.0...v1.36.1) (2026-07-07)


### Bug Fixes

* default API_HOST to DEPLOY_FQDN so remote deploys activate the access gate ([a223c40](https://github.com/cboulanger/zotero-rag/commit/a223c40cda9b275fa06a163009383aa20ca5bd7f))

# [1.36.0](https://github.com/cboulanger/zotero-rag/compare/v1.35.0...v1.36.0) (2026-07-06)


### Features

* Zotero-key authentication: per-user identity, access gate, plugin wizard ([#39](https://github.com/cboulanger/zotero-rag/issues/39)) ([7499215](https://github.com/cboulanger/zotero-rag/commit/74992154fd07d7aa2347a55f862ff0bd1e5e80cc))

# [1.35.0](https://github.com/cboulanger/zotero-rag/compare/v1.34.0...v1.35.0) (2026-07-05)


### Bug Fixes

* add Zotero-API-Version header and harden key validator robustness ([cbe4f08](https://github.com/cboulanger/zotero-rag/commit/cbe4f086ecfebdd4483a4e7ab3873019873983ed))
* comply with event-loop rule in autoindex handlers ([9bda47a](https://github.com/cboulanger/zotero-rag/commit/9bda47aaed9a337b894fb55a48e3658e6565c06b))
* enforce 0600 perms on autoindex keys file and skip no-op remove write ([9e38eea](https://github.com/cboulanger/zotero-rag/commit/9e38eea31c7e1da590ff74a2a4785d9b6ac6d505))
* keep auto-index keys on transient validation failures (avoid wiping store on outage) ([50b4998](https://github.com/cboulanger/zotero-rag/commit/50b499860221fef4e5652895fd4794d76b49de1a))
* open pref-pane links in browser, add key-portal links, simplify server help text ([fa6afc6](https://github.com/cboulanger/zotero-rag/commit/fa6afc61324e6378fd2e5ef439570073d8a52616))
* skip env-dependent tests instead of failing when environment is unavailable ([cb013d3](https://github.com/cboulanger/zotero-rag/commit/cb013d369f6bfbda08ab1b37bbaa578bfd34b452))
* wire key last_status updates and refresh stale cron-indexing doc ([28b8edf](https://github.com/cboulanger/zotero-rag/commit/28b8edf481e6399c56ae18b781fcde6b19460ebf))


### Features

* add /api/autoindex/keys endpoints ([bcf643b](https://github.com/cboulanger/zotero-rag/commit/bcf643b5436e1aef221fdbe969218082ef8fd77e))
* add CLI to onboard a read-only auto-index key ([4a633f0](https://github.com/cboulanger/zotero-rag/commit/4a633f0f23e3360f66f68c8db50c8903997f1330))
* add cryptography dep and autoindex settings ([c4555e1](https://github.com/cboulanger/zotero-rag/commit/c4555e13865e16628a9ddf4489df2f5d23577140))
* add Fernet-encrypted auto-index key store ([464a42f](https://github.com/cboulanger/zotero-rag/commit/464a42f9dd64a08ad0be4cde5ac3b5175edae039))
* add read-only Zotero key validator with target resolution ([a33b04b](https://github.com/cboulanger/zotero-rag/commit/a33b04b12687c3d15c1804cb366c9f5f10de8f0c))
* cron indexer resolves per-library keys with re-validation and dedup ([ba8a83b](https://github.com/cboulanger/zotero-rag/commit/ba8a83b3b2b0accca05a5582c20518e77536bf84))
* expose auto-index registry + per-library totals on root endpoint ([7c965a6](https://github.com/cboulanger/zotero-rag/commit/7c965a6ea67895e6d8eae4da77924e72d14a24a1))
* plugin UI to submit read-only auto-index keys ([9fb2e63](https://github.com/cboulanger/zotero-rag/commit/9fb2e6359ce80a8b7273c9f87a76f5a0a0a3603c))
* show cron-indexing status in library lists ([baf663c](https://github.com/cboulanger/zotero-rag/commit/baf663c7bf774d8195b07ad93c56f8718238b3b4))

# [1.34.0](https://github.com/cboulanger/zotero-rag/compare/v1.33.9...v1.34.0) (2026-06-30)


### Bug Fixes

* prevent test-triggered OOM and isolate indexing memory via subprocesses ([37b9c12](https://github.com/cboulanger/zotero-rag/commit/37b9c12ea06ad69f80a7338a3ee6d760234b4f8d))


### Features

* make embedding batch size configurable + document INDEX_BATCH_SIZE ([04ab848](https://github.com/cboulanger/zotero-rag/commit/04ab848fe9f78b9a500c2235b562c3e548f7cb96))

## [1.33.9](https://github.com/cboulanger/zotero-rag/compare/v1.33.8...v1.33.9) (2026-06-28)


### Performance Improvements

* configure kreuzberg to use Tesseract-only OCR with eng/deu/fra/spa ([eeadacc](https://github.com/cboulanger/zotero-rag/commit/eeadacc0eda610662da271c25bf9367e456ea32a))

## [1.33.8](https://github.com/cboulanger/zotero-rag/compare/v1.33.7...v1.33.8) (2026-06-28)


### Performance Improvements

* adaptive gc/malloc_trim triggered by RSS threshold instead of item count [skip ci] ([a6c63f2](https://github.com/cboulanger/zotero-rag/commit/a6c63f20ab53ca03dd24e602ef188494c566d4af))

## [1.33.7](https://github.com/cboulanger/zotero-rag/compare/v1.33.6...v1.33.7) (2026-06-28)


### Performance Improvements

* spill full item list to JSONL to avoid holding 3-4 GB in memory [skip ci] ([f112969](https://github.com/cboulanger/zotero-rag/commit/f1129699de60cd6b0151ccb0207409fdb9eea74d))

## [1.33.6](https://github.com/cboulanger/zotero-rag/compare/v1.33.5...v1.33.6) (2026-06-27)


### Performance Improvements

* call gc.collect + malloc_trim every 10 items during full sync [skip ci] ([237d26b](https://github.com/cboulanger/zotero-rag/commit/237d26ba72408fa13a5950e499a9713ca17aeb97))

## [1.33.5](https://github.com/cboulanger/zotero-rag/compare/v1.33.4...v1.33.5) (2026-06-27)


### Bug Fixes

* **dedup:** self-heal orphaned dedup records so chunkless items can re-index ([cdcf9bf](https://github.com/cboulanger/zotero-rag/commit/cdcf9bf24fc1abf216f78fb1dfa726fa624ed1b3))


### Performance Improvements

* eliminate N+1 Zotero API calls in full-sync attachment filtering [skip ci] ([7ea9b68](https://github.com/cboulanger/zotero-rag/commit/7ea9b68a0b48fc7c452c834e8119c596cfd2bab1))

## [1.33.4](https://github.com/cboulanger/zotero-rag/compare/v1.33.3...v1.33.4) (2026-06-27)


### Bug Fixes

* **cron:** surface embedding auth failures instead of indexing zero chunks silently ([dd5b637](https://github.com/cboulanger/zotero-rag/commit/dd5b6377818e17526b704c9301079221afe230d8))
* **deploy:** write systemd service secrets to a root-only env file, not inline ([b1d8b7b](https://github.com/cboulanger/zotero-rag/commit/b1d8b7b49062fda3fb4beaf5c2a270812000af1f))

## [1.33.3](https://github.com/cboulanger/zotero-rag/compare/v1.33.2...v1.33.3) (2026-06-27)


### Bug Fixes

* advance last_indexed_version over non-indexable items in incremental mode ([98e93c3](https://github.com/cboulanger/zotero-rag/commit/98e93c3f48bbbf60106d239ae6c5194e5ff50370))

## [1.33.2](https://github.com/cboulanger/zotero-rag/compare/v1.33.1...v1.33.2) (2026-06-25)


### Bug Fixes

* update test_incremental_indexing for smart-sync full mode ([973defd](https://github.com/cboulanger/zotero-rag/commit/973defde378cbf30a0c0069a4ba3169e4000f2b4))

## [1.33.1](https://github.com/cboulanger/zotero-rag/compare/v1.33.0...v1.33.1) (2026-06-25)


### Bug Fixes

* replace wipe-and-rebuild full indexing with safe smart sync ([4dcc438](https://github.com/cboulanger/zotero-rag/commit/4dcc438d187b75736f47b7dcf53cc762bd1710e7))

# [1.33.0](https://github.com/cboulanger/zotero-rag/compare/v1.32.1...v1.33.0) (2026-06-25)


### Bug Fixes

* probe embedding API live when no cached rate-limit headers exist ([6dc7662](https://github.com/cboulanger/zotero-rag/commit/6dc76624b30856009eaacfe557c744953e754f7c))
* show green checkmark for libraries with all available items indexed ([3828d3a](https://github.com/cboulanger/zotero-rag/commit/3828d3ab58428b7257fc88f7dc55bbe459cb9b23))


### Features

* **cron:** force full re-index on interrupted runs and under-indexed libraries ([80ebbbe](https://github.com/cboulanger/zotero-rag/commit/80ebbbe33aa21258b9ccc466b9713fd466f8c7d8))

## [1.32.1](https://github.com/cboulanger/zotero-rag/compare/v1.32.0...v1.32.1) (2026-06-23)


### Bug Fixes

* persist rate-limit headers to cron_status.json for cross-process visibility ([68fbd73](https://github.com/cboulanger/zotero-rag/commit/68fbd739583633bdc3ed9cde66293687a6766532))

# [1.32.0](https://github.com/cboulanger/zotero-rag/compare/v1.31.2...v1.32.0) (2026-06-23)


### Bug Fixes

* live-update chunks_added in cron indexer status during indexing ([152a9dd](https://github.com/cboulanger/zotero-rag/commit/152a9dd71bb76eff84fe9a6e63f10678429ebd01))


### Features

* replace exponential backoff with quota-aware rate-limit handling in cron indexer ([433593b](https://github.com/cboulanger/zotero-rag/commit/433593b4f0f6b43ce81eb92b9bc2e137ee7855f6))

## [1.31.2](https://github.com/cboulanger/zotero-rag/compare/v1.31.1...v1.31.2) (2026-06-23)


### Bug Fixes

* match container network MTU to host interface on creation ([8cdc86e](https://github.com/cboulanger/zotero-rag/commit/8cdc86ebae3af9ccf2e00c161e799410b7c02a0e))

## [1.31.1](https://github.com/cboulanger/zotero-rag/compare/v1.31.0...v1.31.1) (2026-06-23)


### Bug Fixes

* copy bin/ into container image and set DATA_PATH=/data ([864b294](https://github.com/cboulanger/zotero-rag/commit/864b2940c5b2a8169ca8d35cca3bc8fa31f088c1))

# [1.31.0](https://github.com/cboulanger/zotero-rag/compare/v1.30.0...v1.31.0) (2026-06-23)


### Features

* add headless cron indexing via Zotero web API ([04ac305](https://github.com/cboulanger/zotero-rag/commit/04ac305f00cab669085ef51c39200b39e46317bb))

# [1.30.0](https://github.com/cboulanger/zotero-rag/compare/v1.29.1...v1.30.0) (2026-06-17)


### Features

* replace hardcoded KISSKI model list with dynamic API fetch ([306cd73](https://github.com/cboulanger/zotero-rag/commit/306cd733ebf23b28a665323027d2e678e22a45b6))

## [1.29.1](https://github.com/cboulanger/zotero-rag/compare/v1.29.0...v1.29.1) (2026-05-19)


### Bug Fixes

* prevent duplicate "RAG Results" saved search creation ([ab0c265](https://github.com/cboulanger/zotero-rag/commit/ab0c2652c986b4731694666ec9de097e313cd75e)), closes [#32](https://github.com/cboulanger/zotero-rag/issues/32)

# [1.29.0](https://github.com/cboulanger/zotero-rag/compare/v1.28.0...v1.29.0) (2026-05-14)


### Features

* Add per-model demand indicator for KISSKI remote endpoints ([1694a1b](https://github.com/cboulanger/zotero-rag/commit/1694a1bc4fdeea311e595af8d71736128c851dbb))

# [1.28.0](https://github.com/cboulanger/zotero-rag/compare/v1.27.0...v1.28.0) (2026-05-13)


### Features

* Add RAG Results saved search and specific note tags ([6432749](https://github.com/cboulanger/zotero-rag/commit/643274973dff109147eefd61a07342e3a203a5fe))

# [1.27.0](https://github.com/cboulanger/zotero-rag/compare/v1.26.0...v1.27.0) (2026-05-12)


### Features

* Add advanced query options and in-plugin trace debugging ([e5b2b99](https://github.com/cboulanger/zotero-rag/commit/e5b2b99bd05796b3ab81f8a4844ed103f47d46d7))
* Add RAG query execution trace API and debugging script ([044ebf1](https://github.com/cboulanger/zotero-rag/commit/044ebf143219c47c0208536bdf3d7323e59bc79b))

# [1.26.0](https://github.com/cboulanger/zotero-rag/compare/v1.25.1...v1.26.0) (2026-05-12)


### Bug Fixes

* Correct metadata-only routing and add RAG fallback for content questions ([b11aa29](https://github.com/cboulanger/zotero-rag/commit/b11aa290aec3264cab76c9278ae2b422d2b8a152))
* Tag generated notes with 'RAG' metadata tag ([9b2a09b](https://github.com/cboulanger/zotero-rag/commit/9b2a09b994da462f842b5c9357215f75f8ff7d9c))


### Features

* Multi-model LLM selection per preset with dialog picker ([0e18a36](https://github.com/cboulanger/zotero-rag/commit/0e18a360d7cedf19cf59371eb4ac394e15a2c9ea))

## [1.25.1](https://github.com/cboulanger/zotero-rag/compare/v1.25.0...v1.25.1) (2026-05-12)


### Bug Fixes

* Batch cross-library chunk copy to prevent Qdrant timeout on large docs ([da7eebf](https://github.com/cboulanger/zotero-rag/commit/da7eebfc711b36e2a7e39c4e5f91d32382371c79))
* Default to 1 uvicorn worker to fix async task polling (closes [#30](https://github.com/cboulanger/zotero-rag/issues/30)) ([24709b8](https://github.com/cboulanger/zotero-rag/commit/24709b8e00c0b5a93b84df9cae0f2566038aa00a))
* Reduce Qdrant upsert batch size and add retry with exponential backoff ([a7bca14](https://github.com/cboulanger/zotero-rag/commit/a7bca14e1ecd61c7c1a1f9532239e5a8b797a131))
* smaller check-indexed batches and per-phase progress labels (closes [#29](https://github.com/cboulanger/zotero-rag/issues/29)) ([c583711](https://github.com/cboulanger/zotero-rag/commit/c5837119122c564361ccdc504ce0c55e40c4c10b))

# [1.25.0](https://github.com/cboulanger/zotero-rag/compare/v1.24.0...v1.25.0) (2026-05-12)


### Features

* Add indexing report note with doc-type breakdown and auto-open ([2aad88c](https://github.com/cboulanger/zotero-rag/commit/2aad88c76cccaff84d1aa0cf5f95b7b4de193f16))

# [1.24.0](https://github.com/cboulanger/zotero-rag/compare/v1.23.0...v1.24.0) (2026-05-08)


### Bug Fixes

* Remove spinner after task ends ([1593ecf](https://github.com/cboulanger/zotero-rag/commit/1593ecf558d9629afae8dcc4c1068c96a5ddcc35))


### Features

* Add library visibility filter to settings ([46ac231](https://github.com/cboulanger/zotero-rag/commit/46ac231e391f725020b548d0dd8d70baf2dd2fba))

# [1.23.0](https://github.com/cboulanger/zotero-rag/compare/v1.22.0...v1.23.0) (2026-05-07)


### Features

* async document upload with live server-side progress reporting ([0f8a13d](https://github.com/cboulanger/zotero-rag/commit/0f8a13d59e8df0756dc3b28d6e3cb7473b205404))

# [1.22.0](https://github.com/cboulanger/zotero-rag/compare/v1.21.1...v1.22.0) (2026-05-07)


### Bug Fixes

* address indexing reliability — event loop blocking, cache migration, batched metadata, retry logic ([0af7d2d](https://github.com/cboulanger/zotero-rag/commit/0af7d2dfec4f71dc377c3d19914433458c297316))
* guard llm_service.model_name against non-string values in orchestrator ([f420965](https://github.com/cboulanger/zotero-rag/commit/f4209653e7bc21642dc9e143d1750bf39cad36ab))


### Features

* enrich generated note footer with model, agents, and document counts ([1f00ae7](https://github.com/cboulanger/zotero-rag/commit/1f00ae7af2e01bb68faabddc615e99192ce703d3))

## [1.21.1](https://github.com/cboulanger/zotero-rag/compare/v1.21.0...v1.21.1) (2026-05-05)


### Bug Fixes

* restore rate limit widget visibility during active indexing operations ([57891ae](https://github.com/cboulanger/zotero-rag/commit/57891aec103e57245f1702c64687e1e841a2c195))

# [1.21.0](https://github.com/cboulanger/zotero-rag/compare/v1.20.5...v1.21.0) (2026-05-05)


### Features

* query routing and agent dispatch for bibliographic metadata queries ([4adf045](https://github.com/cboulanger/zotero-rag/commit/4adf0452a45c153dca65e829baa5e343e5cc696a)), closes [#19](https://github.com/cboulanger/zotero-rag/issues/19)

## [1.20.5](https://github.com/cboulanger/zotero-rag/compare/v1.20.4...v1.20.5) (2026-05-04)


### Bug Fixes

* reject PDFs whose split parts inflate to near original size ([b5df91e](https://github.com/cboulanger/zotero-rag/commit/b5df91ea7928f00d4ecf7d2d08927292f4ff0e41))
* set 8 GB memory limit on kreuzberg container ([138ad91](https://github.com/cboulanger/zotero-rag/commit/138ad91bd93638f6e4b60dcacfc333eafc805963))

## [1.20.4](https://github.com/cboulanger/zotero-rag/compare/v1.20.3...v1.20.4) (2026-05-04)


### Bug Fixes

* log exception traceback on query 500 errors ([a31b34a](https://github.com/cboulanger/zotero-rag/commit/a31b34a13f4d87897b653af5f80bb88245a29be3))
* resolve incomplete indexing blocking question submission (closes [#24](https://github.com/cboulanger/zotero-rag/issues/24)) ([158f888](https://github.com/cboulanger/zotero-rag/commit/158f88829ee0175fd2f42a6d7610011efb96517c))

## [1.20.3](https://github.com/cboulanger/zotero-rag/compare/v1.20.2...v1.20.3) (2026-05-03)


### Bug Fixes

* enable OCR in kreuzberg and split large PDFs to prevent OOM kills ([e21cf39](https://github.com/cboulanger/zotero-rag/commit/e21cf393b74c42cb1ec98cd3120c60547797cec4))

## [1.20.2](https://github.com/cboulanger/zotero-rag/compare/v1.20.1...v1.20.2) (2026-05-02)


### Bug Fixes

* sync toolbar button badge with Fix Unavailable count ([0f7878b](https://github.com/cboulanger/zotero-rag/commit/0f7878b1a34c9925f7cc2bfa5779fe8cb158f1c6))

## [1.20.1](https://github.com/cboulanger/zotero-rag/compare/v1.20.0...v1.20.1) (2026-05-02)


### Bug Fixes

* correct indexing counts, skip tracking, and Fix Unavailable completeness ([6f83346](https://github.com/cboulanger/zotero-rag/commit/6f8334660fad13df5f1a159b485eb02c0e804a9c))

# [1.20.0](https://github.com/cboulanger/zotero-rag/compare/v1.19.8...v1.20.0) (2026-05-02)


### Bug Fixes

* suppress stack trace for transient Qdrant disconnects in check-indexed ([2a733a9](https://github.com/cboulanger/zotero-rag/commit/2a733a9b79a5ae5622bc19436132cf30d7fbf5c2))


### Features

* persist check-indexed item cache across server restarts ([f8d3a5c](https://github.com/cboulanger/zotero-rag/commit/f8d3a5ced280b861f902350128eb80092b208b84))

## [1.19.8](https://github.com/cboulanger/zotero-rag/compare/v1.19.7...v1.19.8) (2026-05-01)


### Bug Fixes

* log upload file size in human-readable format ([24d2533](https://github.com/cboulanger/zotero-rag/commit/24d25332e099eec112b487179dc03edd1b3db71f))
* replace fixed kreuzberg timeout with size-scaled single attempt ([821967e](https://github.com/cboulanger/zotero-rag/commit/821967ed1a246c873b16bddc17624d8593592b84))

## [1.19.7](https://github.com/cboulanger/zotero-rag/compare/v1.19.6...v1.19.7) (2026-05-01)


### Bug Fixes

* Four indexing correctness/performance fixes for large libraries ([394950f](https://github.com/cboulanger/zotero-rag/commit/394950ffe0d80385daf8ccb4e6897bf8cd90d23b))
* suppress kreuzberg stack traces in logs; auto-start podman machine ([545e5d6](https://github.com/cboulanger/zotero-rag/commit/545e5d6345ad009637d68696af86d52bcc3b6ac8))

## [1.19.6](https://github.com/cboulanger/zotero-rag/compare/v1.19.5...v1.19.6) (2026-04-30)


### Bug Fixes

* Use author/year from vector index when Zotero API metadata is unavailable ([2135274](https://github.com/cboulanger/zotero-rag/commit/213527465fae7b6b69bbe5b05c8d41a186ba561f)), closes [#22](https://github.com/cboulanger/zotero-rag/issues/22)

## [1.19.5](https://github.com/cboulanger/zotero-rag/compare/v1.19.4...v1.19.5) (2026-04-30)


### Bug Fixes

* Count unique parent items in countIndexableAttachments, not total attachments ([9d14dae](https://github.com/cboulanger/zotero-rag/commit/9d14dae1bf2c6c0033b4559f2e3297dc0e0e2d70))

## [1.19.4](https://github.com/cboulanger/zotero-rag/compare/v1.19.3...v1.19.4) (2026-04-30)


### Bug Fixes

* Reconcile total_items_indexed after reindex via live vector store count ([9bcc3b9](https://github.com/cboulanger/zotero-rag/commit/9bcc3b9fd74495383341f6a7dcaadc94afbcbe2e))

## [1.19.3](https://github.com/cboulanger/zotero-rag/compare/v1.19.2...v1.19.3) (2026-04-30)


### Bug Fixes

* Add TimeoutStartSec=300 to override systemd's 90s ExecStartPre limit ([49c617f](https://github.com/cboulanger/zotero-rag/commit/49c617fa94e481ae97fd00ed85b5a4d9e8aad856))

## [1.19.2](https://github.com/cboulanger/zotero-rag/compare/v1.19.1...v1.19.2) (2026-04-30)


### Bug Fixes

* Correct cache invalidation logic for check-indexed ([1bfbf84](https://github.com/cboulanger/zotero-rag/commit/1bfbf8401c508d53759b5b21d88cf53228cc353c))
* Increase Qdrant and app readiness timeouts to handle slow collection recovery ([dcdb5a0](https://github.com/cboulanger/zotero-rag/commit/dcdb5a09756eedb3c8e977498c8dfe348dac129a))

## [1.19.1](https://github.com/cboulanger/zotero-rag/compare/v1.19.0...v1.19.1) (2026-04-30)


### Bug Fixes

* Cache check-indexed results server-side, invalidate on full reindex ([a29b793](https://github.com/cboulanger/zotero-rag/commit/a29b793d9769dbfedee289fd2bc07e4f15f66a98)), closes [#18](https://github.com/cboulanger/zotero-rag/issues/18)

# [1.19.0](https://github.com/cboulanger/zotero-rag/compare/v1.18.0...v1.19.0) (2026-04-30)


### Bug Fixes

* Increase retries waiting for qdrant sidecar ([99f2ce2](https://github.com/cboulanger/zotero-rag/commit/99f2ce218b3def971976eca07e610be772af4659))


### Features

* Add OpenAlex → Zotero bulk import script ([f564c00](https://github.com/cboulanger/zotero-rag/commit/f564c0010a5d3c7506620a8c78470f909b8b1b61))
* Add public web UI for unauthenticated RAG queries ([276dedd](https://github.com/cboulanger/zotero-rag/commit/276dedd66df4b20370f69b22678831415ddfb69a))

# [1.18.0](https://github.com/cboulanger/zotero-rag/compare/v1.17.9...v1.18.0) (2026-04-29)


### Bug Fixes

* Handle kreuzberg ReadError, add auto-update, raise UV_HTTP_TIMEOUT ([3687e77](https://github.com/cboulanger/zotero-rag/commit/3687e7710c8cf4ae78a355e824c6885652632cd6))


### Features

* Add app icon and toolbar button ([654ae3e](https://github.com/cboulanger/zotero-rag/commit/654ae3ee792c23d0b89cd23129e850d7bcd0c94f))

## [1.17.9](https://github.com/cboulanger/zotero-rag/compare/v1.17.8...v1.17.9) (2026-04-29)


### Bug Fixes

* Skip check-indexed for items already confirmed as needing indexing ([0803887](https://github.com/cboulanger/zotero-rag/commit/0803887d68648b216dc3f577f470c012f8fbea3d))
* Wait for Qdrant to be ready before starting main container ([319e69b](https://github.com/cboulanger/zotero-rag/commit/319e69bc0104dc72d587fce95cf32f8ae207dae8))

## [1.17.8](https://github.com/cboulanger/zotero-rag/compare/v1.17.7...v1.17.8) (2026-04-29)


### Bug Fixes

* Fix feedback container css ([c47bd2e](https://github.com/cboulanger/zotero-rag/commit/c47bd2ec9bb0d949eb2fb5e27ce2fe2c66e13792))
* Persist check-indexed results to version cache after each batch ([8d0165e](https://github.com/cboulanger/zotero-rag/commit/8d0165e471fb953897cb6fc592f4f6ac9c855e26))

## [1.17.7](https://github.com/cboulanger/zotero-rag/compare/v1.17.6...v1.17.7) (2026-04-29)


### Bug Fixes

* fix startup error ([a46839c](https://github.com/cboulanger/zotero-rag/commit/a46839ce47f4c61f33ff39e9b27f4255dc7dfc9d))

## [1.17.6](https://github.com/cboulanger/zotero-rag/compare/v1.17.5...v1.17.6) (2026-04-29)


### Bug Fixes

* Fix Kreuzberg timeout issues ([db9a628](https://github.com/cboulanger/zotero-rag/commit/db9a628e6ddb23da523cabad4f43f8805bb644f1))
* Make upload limit configurable and display failed upload size ([5d6a607](https://github.com/cboulanger/zotero-rag/commit/5d6a60769a7d8e5b60beb0b2f5a4ab74003b9f8c))

## [1.17.5](https://github.com/cboulanger/zotero-rag/compare/v1.17.4...v1.17.5) (2026-04-28)


### Bug Fixes

* **ci:** Pre-install en_core_web_sm and remove obsolete retry tests ([53f7b94](https://github.com/cboulanger/zotero-rag/commit/53f7b943323b819d37382a711e6df264a431e565))
* Fix tests ([e6fa9e7](https://github.com/cboulanger/zotero-rag/commit/e6fa9e7c38f8cb59da7170e35e4f71a0e547f848))
* Pre-install en_core_web_sm in Docker builder and fix runtime fallback ([dd15502](https://github.com/cboulanger/zotero-rag/commit/dd155026ce6ae778cefbdc79b6163fb5d59ac411)), closes [#16](https://github.com/cboulanger/zotero-rag/issues/16)
* Resolve check-indexed timeout for large libraries ([#13](https://github.com/cboulanger/zotero-rag/issues/13)) ([e17c8a2](https://github.com/cboulanger/zotero-rag/commit/e17c8a273e889000a7fe06794b3ee421278cf8a1))
* Skip download for non-stored attachments to suppress spurious errors ([#14](https://github.com/cboulanger/zotero-rag/issues/14)) ([889a44f](https://github.com/cboulanger/zotero-rag/commit/889a44ff056e7a0a3e0009f8a59854836d0f7f73))
* Treat kreuzberg 422 ParsingError as skipped_parse_error and flag in Fix Unavailable ([427f7e3](https://github.com/cboulanger/zotero-rag/commit/427f7e3fae43d2631bca272823e28b7b6b57dbab)), closes [#15](https://github.com/cboulanger/zotero-rag/issues/15)

## [1.17.4](https://github.com/cboulanger/zotero-rag/compare/v1.17.3...v1.17.4) (2026-04-26)


### Bug Fixes

* Fix process_attachment_bytes  returned a bare int 0 instead of AttachmentProcessingResult. ([41c1378](https://github.com/cboulanger/zotero-rag/commit/41c1378d70066322ed472742ffb267405323118f))

## [1.17.3](https://github.com/cboulanger/zotero-rag/compare/v1.17.2...v1.17.3) (2026-04-26)


### Bug Fixes

* Add Qdrant payload indexes and client-side circuit breaker to prevent check-indexed overload ([f773881](https://github.com/cboulanger/zotero-rag/commit/f773881a16b7ff1778ced48758c8abece86d7099))

## [1.17.2](https://github.com/cboulanger/zotero-rag/compare/v1.17.1...v1.17.2) (2026-04-26)


### Bug Fixes

* Add configurable timeout and retry to Qdrant check-indexed scroll ([1cdf0d7](https://github.com/cboulanger/zotero-rag/commit/1cdf0d7219ecfc7aa5f151dc8943a1a0ea6c3e5c))
* Fix three indexing bugs in remote indexer and dialog ([9db8224](https://github.com/cboulanger/zotero-rag/commit/9db822433bbadc187008b07ed0094fa9c923d533))
* Log unhandled exceptions that produce silent HTTP 500 responses ([0059b0e](https://github.com/cboulanger/zotero-rag/commit/0059b0e5d2dd97f03122f39d08d0fde72df9a6e0))

## [1.17.1](https://github.com/cboulanger/zotero-rag/compare/v1.17.0...v1.17.1) (2026-04-26)


### Bug Fixes

* Fix log noise ([f82f3e8](https://github.com/cboulanger/zotero-rag/commit/f82f3e8023eba81147f8bd152a2e3d8eeba8858e))
* Fix wrong library id being used when counting unavailable items ([034a2c3](https://github.com/cboulanger/zotero-rag/commit/034a2c364748e09576c9ddb11fac63d6a2be1604))

# [1.17.0](https://github.com/cboulanger/zotero-rag/compare/v1.16.1...v1.17.0) (2026-04-25)


### Features

* Add cross-library deduplication for vector store ([9b6bd40](https://github.com/cboulanger/zotero-rag/commit/9b6bd406331b49c4e2d15f21ab8b5339e3411415))

## [1.16.1](https://github.com/cboulanger/zotero-rag/compare/v1.16.0...v1.16.1) (2026-04-24)


### Bug Fixes

* Batch upsert requests to avoid timeouts ([4d6fde7](https://github.com/cboulanger/zotero-rag/commit/4d6fde725209284eb30775ab1b4965ad755db9a5))

# [1.16.0](https://github.com/cboulanger/zotero-rag/compare/v1.15.3...v1.16.0) (2026-04-24)


### Bug Fixes

* Fix Fix tool: Fixing via resolver must match file type ([7e84b6d](https://github.com/cboulanger/zotero-rag/commit/7e84b6d7975168cc405dc74a8c09c969e444a768)), closes [#10](https://github.com/cboulanger/zotero-rag/issues/10)
* Fix stale content in fix tool when switching libraries ([948edf3](https://github.com/cboulanger/zotero-rag/commit/948edf3606a66e365f4e670a4cc85c43eb5bae3c))


### Features

* Implement Urge user to fix attachment problems before indexing ([ab7fd68](https://github.com/cboulanger/zotero-rag/commit/ab7fd680406bf5bada5366792dd14ff5b84228a3)), closes [#7](https://github.com/cboulanger/zotero-rag/issues/7)

## [1.15.3](https://github.com/cboulanger/zotero-rag/compare/v1.15.2...v1.15.3) (2026-04-24)


### Bug Fixes

* Fix missing warning about unsecured connection ([5e052bb](https://github.com/cboulanger/zotero-rag/commit/5e052bb3fb06f8342d69030fe117917695f5affd))

## [1.15.2](https://github.com/cboulanger/zotero-rag/compare/v1.15.1...v1.15.2) (2026-04-23)


### Bug Fixes

* Fix ltems with broken linked file are not listed in fix-unavailable-attachments tool ([87eb98d](https://github.com/cboulanger/zotero-rag/commit/87eb98d133c6d766ec544933e0e3d7df46368cd0))

## [1.15.1](https://github.com/cboulanger/zotero-rag/compare/v1.15.0...v1.15.1) (2026-04-22)


### Bug Fixes

* Fix data path ([1b8e06b](https://github.com/cboulanger/zotero-rag/commit/1b8e06bf58a8844b6a27a5caaf3d7213a6c03de2))

# [1.15.0](https://github.com/cboulanger/zotero-rag/compare/v1.14.0...v1.15.0) (2026-04-22)


### Bug Fixes

* Fix various UI bugs ([17016b9](https://github.com/cboulanger/zotero-rag/commit/17016b99be0184362ba16ef4981343ff315e41f4))
* Use "u{user id}" instead of "1" as library id on the server to distinguish user libraries ([c5f1308](https://github.com/cboulanger/zotero-rag/commit/c5f1308c93f784d816f4f792dbbda4c79941a58f))


### Features

* Add fragment context for LLM ([7a22d23](https://github.com/cboulanger/zotero-rag/commit/7a22d23aad5ed6751097565f4261daab0a7de42f))

# [1.14.0](https://github.com/cboulanger/zotero-rag/compare/v1.13.0...v1.14.0) (2026-04-22)


### Bug Fixes

* fix failed attachment cache, don't calculate library size ([e3f7a11](https://github.com/cboulanger/zotero-rag/commit/e3f7a11b274d5c80592a6e3eb65d867283a31509))


### Features

* Adapt non-functional libraries API ([09707a4](https://github.com/cboulanger/zotero-rag/commit/09707a425783175b5b6999483d1a86094ed18ae4))

# [1.13.0](https://github.com/cboulanger/zotero-rag/compare/v1.12.2...v1.13.0) (2026-04-22)


### Bug Fixes

* Add missing dependency ([8a7b807](https://github.com/cboulanger/zotero-rag/commit/8a7b807c6f4ba5a19f6b43b99541e785d811479f))
* Fix tests ([4c8e3e4](https://github.com/cboulanger/zotero-rag/commit/4c8e3e4b04eedb39bbdf85012abbf06e3cc80214))
* Make registration file edits thread-safe ([fb5367e](https://github.com/cboulanger/zotero-rag/commit/fb5367ec4e0844b815560a7c7025763f3f7e0500))


### Features

* Add rate limit widget to RAG dialog ([f9df5c7](https://github.com/cboulanger/zotero-rag/commit/f9df5c79e95e91ea63eba3a46d738ab025be1151))

## [1.12.2](https://github.com/cboulanger/zotero-rag/compare/v1.12.1...v1.12.2) (2026-04-21)


### Bug Fixes

* Fix rate limit errors do not lead to hard fail and errors are not displayed ([a9679e8](https://github.com/cboulanger/zotero-rag/commit/a9679e80c6311c2007cd24d5e00cb2ff0131cb0d))

## [1.12.1](https://github.com/cboulanger/zotero-rag/compare/v1.12.0...v1.12.1) (2026-04-21)


### Bug Fixes

*  Fix indexing gets stuck on embedder rate limits ([8f6f66d](https://github.com/cboulanger/zotero-rag/commit/8f6f66d620611c74755de1ac83be5bb75655287b))

# [1.12.0](https://github.com/cboulanger/zotero-rag/compare/v1.11.0...v1.12.0) (2026-04-21)


### Features

* Add library and user registration ([2e5377c](https://github.com/cboulanger/zotero-rag/commit/2e5377cdc0d292359c629522a6765f1abd6777f5))

# [1.11.0](https://github.com/cboulanger/zotero-rag/compare/v1.10.3...v1.11.0) (2026-04-21)


### Bug Fixes

* Any selected unindexed library  enforces indexing ([3fcafaa](https://github.com/cboulanger/zotero-rag/commit/3fcafaa6d8f6f1ac957c89928a4c0d65cf99e739))
* Scroll first selected library into view when dialog opens ([d0cdc8d](https://github.com/cboulanger/zotero-rag/commit/d0cdc8d5f25dd31519cb320e4787166c55e1093b))


### Features

* Delete fragments when Zotero item is deleted ([29547db](https://github.com/cboulanger/zotero-rag/commit/29547dbd06e59c893e5e430bb2da16faa2084dcc))
* Show indexing info for all libraries right away ([835e6b0](https://github.com/cboulanger/zotero-rag/commit/835e6b0fa37e7c599e342b008fcd89c6891d58d1))

## [1.10.3](https://github.com/cboulanger/zotero-rag/compare/v1.10.2...v1.10.3) (2026-04-21)


### Bug Fixes

* Set longer timeouts for indexing individual documents ([e160a45](https://github.com/cboulanger/zotero-rag/commit/e160a455e16e054a9a25eddee3307f320aed792c))

## [1.10.2](https://github.com/cboulanger/zotero-rag/compare/v1.10.1...v1.10.2) (2026-04-21)


### Bug Fixes

* Fix wrong indexed/cached count ([1aef88d](https://github.com/cboulanger/zotero-rag/commit/1aef88d76a39bb505f3f330123aaf3c04d3c2bcc))

## [1.10.1](https://github.com/cboulanger/zotero-rag/compare/v1.10.0...v1.10.1) (2026-04-21)


### Bug Fixes

* Improvements to the "fix unavailable attachments" tool ([b40eebf](https://github.com/cboulanger/zotero-rag/commit/b40eebf0b589dbc7e955e07505cf1c024638c738))

# [1.10.0](https://github.com/cboulanger/zotero-rag/compare/v1.9.2...v1.10.0) (2026-04-20)


### Features

* Add tool to fix unavailable attachments ([47a1a47](https://github.com/cboulanger/zotero-rag/commit/47a1a47edc5c32c099df12f32bebe0067007c9c7))
* Add UI for number of sources to consider ([67544d5](https://github.com/cboulanger/zotero-rag/commit/67544d5eb5fa39b4b2d4de103686f7e61d2c7fde))
* Index abstracts is there is no attachment ([f3dad16](https://github.com/cboulanger/zotero-rag/commit/f3dad16189fb4d63e5e9359de235f6fb7f7a47d7))
* Open RAG result note in separate window ([610f13f](https://github.com/cboulanger/zotero-rag/commit/610f13fd94cb3f07c7a1233a38baf08cc65cef73))

## [1.9.2](https://github.com/cboulanger/zotero-rag/compare/v1.9.1...v1.9.2) (2026-04-20)


### Bug Fixes

* Fix container startup bug identified by container smoke test ([ecafd71](https://github.com/cboulanger/zotero-rag/commit/ecafd71f28bc0eaf2f330618c627a04711970b3d))

## [1.9.1](https://github.com/cboulanger/zotero-rag/compare/v1.9.0...v1.9.1) (2026-04-20)


### Bug Fixes

* Fix qdrant image name ([46e48dc](https://github.com/cboulanger/zotero-rag/commit/46e48dc772fc108cc97961a60162c73e80bcf687))

# [1.9.0](https://github.com/cboulanger/zotero-rag/compare/v1.8.2...v1.9.0) (2026-04-19)


### Features

* Use separate qdrant container ([2ae4281](https://github.com/cboulanger/zotero-rag/commit/2ae4281707791bcd4d149d360e8eb006cf5d0553))

## [1.8.2](https://github.com/cboulanger/zotero-rag/compare/v1.8.1...v1.8.2) (2026-04-19)


### Bug Fixes

* revert workers ([5000fa7](https://github.com/cboulanger/zotero-rag/commit/5000fa7036030395784d17688f501c39d36bd7eb))

## [1.8.1](https://github.com/cboulanger/zotero-rag/compare/v1.8.0...v1.8.1) (2026-04-19)


### Bug Fixes

* Increase workers ([ad80cc3](https://github.com/cboulanger/zotero-rag/commit/ad80cc33c62512be146b382325ec506983e41d94))

# [1.8.0](https://github.com/cboulanger/zotero-rag/compare/v1.7.1...v1.8.0) (2026-04-19)


### Features

* Indexing speedup, bug fixes ([f48022c](https://github.com/cboulanger/zotero-rag/commit/f48022c4f522541bb4ccd5572f1ef1fa58c9d29d))

## [1.7.1](https://github.com/cboulanger/zotero-rag/compare/v1.7.0...v1.7.1) (2026-04-19)


### Bug Fixes

* Fix CI workflow order ([d2ce6dd](https://github.com/cboulanger/zotero-rag/commit/d2ce6ddbcd39cde069169c11029a2cc9ace9f2b7))

# [1.7.0](https://github.com/cboulanger/zotero-rag/compare/v1.6.0...v1.7.0) (2026-04-19)


### Features

* Allow to switch presets gracefully ([063b943](https://github.com/cboulanger/zotero-rag/commit/063b9435c26220854c94ae108c56c1c9031df5de))

# [1.6.0](https://github.com/cboulanger/zotero-rag/compare/v1.5.2...v1.6.0) (2026-04-18)


### Features

* Allow client to provide own API keys ([d2d23d1](https://github.com/cboulanger/zotero-rag/commit/d2d23d18b57b3b8a2bdeb53688bec0a7f7c37ea9))
* fix CI ([fe1d248](https://github.com/cboulanger/zotero-rag/commit/fe1d248d5c291a95b2d48b2b5dfc82620cc2ed46))

## [1.5.2](https://github.com/cboulanger/zotero-rag/compare/v1.5.1...v1.5.2) (2026-04-17)


### Bug Fixes

* Fix connectivity issues ([b96ffd2](https://github.com/cboulanger/zotero-rag/commit/b96ffd2d08533bf5d2671467de4c56e35693402d))

## [1.5.1](https://github.com/cboulanger/zotero-rag/compare/v1.5.0...v1.5.1) (2026-04-17)


### Bug Fixes

* Fix ci and wrong kreuzberg port ([50b44b0](https://github.com/cboulanger/zotero-rag/commit/50b44b0b145aa5a59759f14c3bec71dd976664ce))

# [1.5.0](https://github.com/cboulanger/zotero-rag/compare/v1.4.1...v1.5.0) (2026-04-17)


### Features

* remove zotero dependency ([#5](https://github.com/cboulanger/zotero-rag/issues/5)) ([212eb31](https://github.com/cboulanger/zotero-rag/commit/212eb3122fab78c3f643acef909451934aa99bb9))

## [1.4.1](https://github.com/cboulanger/zotero-rag/compare/v1.4.0...v1.4.1) (2026-04-17)


### Bug Fixes

* sync plugin addon ID and version across all files ([d2d3a7f](https://github.com/cboulanger/zotero-rag/commit/d2d3a7f41966f32d12351d1e94a6754f935c8c25))

# [1.4.0](https://github.com/cboulanger/zotero-rag/compare/v1.3.0...v1.4.0) (2026-04-16)


### Features

* force new version ([3401f5e](https://github.com/cboulanger/zotero-rag/commit/3401f5e18bfb87ffd0afb55fadb199ad60e84646))

# [1.3.0](https://github.com/cboulanger/zotero-rag/compare/v1.2.0...v1.3.0) (2025-11-17)


### Features

* Download attachments before indexing ([abb8cfb](https://github.com/cboulanger/zotero-rag/commit/abb8cfb57460a1031ca656a2efd77095de54e054))

# [1.2.0](https://github.com/cboulanger/zotero-rag/compare/v1.1.0...v1.2.0) (2025-11-17)


### Features

* use zotero-citation fields for sources ([#2](https://github.com/cboulanger/zotero-rag/issues/2)) ([d4f99b9](https://github.com/cboulanger/zotero-rag/commit/d4f99b93d8fedae96e8ef00d709db394f3c2e804))

# [1.1.0](https://github.com/cboulanger/zotero-rag/compare/v1.0.1...v1.1.0) (2025-11-16)


### Bug Fixes

* correct manifest.json path in version script ([df1e943](https://github.com/cboulanger/zotero-rag/commit/df1e94313634a75e598b1703c2f9d059ff1274f6))


### Features

* optimize indexing and server scripts ([#1](https://github.com/cboulanger/zotero-rag/issues/1)) ([c3087b1](https://github.com/cboulanger/zotero-rag/commit/c3087b1cda7a7eae180ae2ab6778503b6ed51352))

## [1.0.1](https://github.com/cboulanger/zotero-rag/compare/v1.0.0...v1.0.1) (2025-11-12)


### Bug Fixes

* **ci:** Fix release script ([2e603d0](https://github.com/cboulanger/zotero-rag/commit/2e603d0c8825715e0ce2059f91c3a9c1604d309a))

# 1.0.0 (2025-11-12)


### Bug Fixes

* **ci:** fix github action ([c7d7c32](https://github.com/cboulanger/zotero-rag/commit/c7d7c321843961207bb4d619daaf63d02bbfc690))
* **ci:** Fix release workflow ([0a50fdd](https://github.com/cboulanger/zotero-rag/commit/0a50fdd775bd7069dfbd07c6b91eb6963466c688))
* **ci:** remove hard-coded repository url ([d6fbe55](https://github.com/cboulanger/zotero-rag/commit/d6fbe55cfc47ab49b71b21b1ea981529d4ec652c))
* **tests:** Fixed failing backend tests ([4e5f2cf](https://github.com/cboulanger/zotero-rag/commit/4e5f2cfe5b928167dd021ad06617dc5f15344277))


### Features

* setup semantic versioning ([f77f7db](https://github.com/cboulanger/zotero-rag/commit/f77f7dba07b42cfcf020461da975405ec2306366))
* test commit to force a release ([b312b08](https://github.com/cboulanger/zotero-rag/commit/b312b08bbfbddd2eec4ee6402141289ad985fcd2))
