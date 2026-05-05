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
