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
