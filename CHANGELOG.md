# Changelog

## [1.9.1](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.9.0...v1.9.1) (2026-08-29)


### Bug Fixes

* **runtime:** honor disabled profile servers ([8525ce5](https://github.com/lightnow-ai/lightnow-proxy/commit/8525ce50ab380a695a9b6e037738d8f793fecd75))

## [1.9.0](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.8.0...v1.9.0) (2026-08-28)


### Features

* **runtime:** materialize managed profile files ([#49](https://github.com/lightnow-ai/lightnow-proxy/issues/49)) ([c8fcbe9](https://github.com/lightnow-ai/lightnow-proxy/commit/c8fcbe9eb1ecf25f5b2576c80a3d39f63aac0ee5))


### Bug Fixes

* **renovate:** constrain Python dependency names ([#38](https://github.com/lightnow-ai/lightnow-proxy/issues/38)) ([92aed0d](https://github.com/lightnow-ai/lightnow-proxy/commit/92aed0d09f9d986206ca8bc6a4332d363aa97d94))

## [1.8.0](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.7.1...v1.8.0) (2026-08-05)


### Features

* **registry:** present Proxy as secure MCP gateway ([#21](https://github.com/lightnow-ai/lightnow-proxy/issues/21)) ([b431798](https://github.com/lightnow-ai/lightnow-proxy/commit/b431798b71a9b8fcf77aa8dc285fd9cb78fd1ec6))

## [1.7.1](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.7.0...v1.7.1) (2026-08-02)


### Bug Fixes

* **registry:** advertise uvx package runtime ([#28](https://github.com/lightnow-ai/lightnow-proxy/issues/28)) ([f522891](https://github.com/lightnow-ai/lightnow-proxy/commit/f522891431af3eeac96066f9368a5b40fbdd3e73))

## [1.7.0](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.6.0...v1.7.0) (2026-08-02)


### Features

* **protocol:** support MCP 2026 dual-era routing ([#23](https://github.com/lightnow-ai/lightnow-proxy/issues/23)) ([08cd527](https://github.com/lightnow-ai/lightnow-proxy/commit/08cd52713feca4a2d6251aefb8cbbeebd3cd5103))


### Bug Fixes

* **deps:** cap MCP SDK before v2 migration ([#22](https://github.com/lightnow-ai/lightnow-proxy/issues/22)) ([729590c](https://github.com/lightnow-ai/lightnow-proxy/commit/729590ce43876533f33dfdb3ed3e7b5dca34c7e4))

## [1.6.0](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.5.0...v1.6.0) (2026-07-18)


### Features

* **#19:** capture sanitized runtime tool arguments ([ccbe870](https://github.com/lightnow-ai/lightnow-proxy/commit/ccbe870b23ccafb5468bde320096296773960302))
* **telemetry:** capture tool arguments ([437aa1c](https://github.com/lightnow-ai/lightnow-proxy/commit/437aa1c7e6542cb3233706879b4441e2d9e463f6))


### Bug Fixes

* **#18:** leaked CLI session lock during concurrent token refresh ([20008e5](https://github.com/lightnow-ai/lightnow-proxy/commit/20008e5d45e9527882b9bfaf7566bf29f360f7df))
* **auth:** make session lock cleanup cancellation-safe ([c0d9596](https://github.com/lightnow-ai/lightnow-proxy/commit/c0d95962d932f76509520446e90962ed614ac20f))
* **auth:** prevent leaked CLI session refresh locks ([3ddafc3](https://github.com/lightnow-ai/lightnow-proxy/commit/3ddafc349e8a7b007a6c4fe6e11f8a2d913fb4b3))
* **telemetry:** harden argument redaction ([7a13c89](https://github.com/lightnow-ai/lightnow-proxy/commit/7a13c8942c6cecbfaa8f7c2f1cd44bc5506a1a96))

## [1.5.0](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.4.2...v1.5.0) (2026-07-17)


### Features

* **#15:** report update status ([89f3289](https://github.com/lightnow-ai/lightnow-proxy/commit/89f32898aece5f5040d1895b54f68c5cd42b292c))
* report update status ([6577e29](https://github.com/lightnow-ai/lightnow-proxy/commit/6577e2909c252bbd4eb1000649ce1043a85f379e))


### Bug Fixes

* resolve proxy installer from path ([c8dfb2a](https://github.com/lightnow-ai/lightnow-proxy/commit/c8dfb2a5e5d7fbc5019a417f3c37cfd354855f4a))

## [1.4.2](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.4.1...v1.4.2) (2026-07-17)


### Bug Fixes

* **#13:** clear runtime diagnostics ([3751a62](https://github.com/lightnow-ai/lightnow-proxy/commit/3751a62050dfee7fffcf943bfce4c9487c9c269a))
* release cancelled session locks ([7c1dfff](https://github.com/lightnow-ai/lightnow-proxy/commit/7c1dfff2da78622a6baf664d3059409112e1e808))
* report actionable runtime diagnostics ([4201c66](https://github.com/lightnow-ai/lightnow-proxy/commit/4201c665a971fe1753ad554de8e11f4a58a3b007))

## [1.4.1](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.4.0...v1.4.1) (2026-07-17)


### Bug Fixes

* **#11:** use resolved profile client configuration ([cb43612](https://github.com/lightnow-ai/lightnow-proxy/commit/cb436122a34e5a0bcf1234f58f5aa81640d014a9))
* preserve profile aliases end to end ([ef642c2](https://github.com/lightnow-ai/lightnow-proxy/commit/ef642c26eebfcbe01fd34359a52738e6f9452100))
* use resolved profile client configuration ([1728eb6](https://github.com/lightnow-ai/lightnow-proxy/commit/1728eb6fac8e4c6c3a8772884776dcb6ff111027))
* validate profile secret transport ([39ca1f7](https://github.com/lightnow-ai/lightnow-proxy/commit/39ca1f78814bd3af45abcefe1a4dcf4c4f84247b))

## [1.4.0](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.3.0...v1.4.0) (2026-07-15)


### Features

* **#6:** bind proxy connections to named sessions ([ef20735](https://github.com/lightnow-ai/lightnow-proxy/commit/ef20735036676049df3bf30be5440081401d1c40))
* **#8:** device control plane ([b1c6b49](https://github.com/lightnow-ai/lightnow-proxy/commit/b1c6b49d8fac86202d0c83e2d85a65b259b88ccf))
* bind proxy connections to named sessions ([25f8785](https://github.com/lightnow-ai/lightnow-proxy/commit/25f8785a711d47cc013086ba7c69722b300059fe))
* **devices:** report local proxy presence ([fa85d44](https://github.com/lightnow-ai/lightnow-proxy/commit/fa85d44f89a7ae813a8cf5010a04d184b1ebbfc7))


### Bug Fixes

* acquire session lock asynchronously ([1222eb7](https://github.com/lightnow-ai/lightnow-proxy/commit/1222eb7db0a884e77cf8e95fe8fcca4e26737e15))


### Documentation

* **devices:** document presence reporting ([d41696f](https://github.com/lightnow-ai/lightnow-proxy/commit/d41696f67c8b25f5729e96cfd455f0aeaaa87319))

## [1.3.0](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.2.0...v1.3.0) (2026-07-12)


### Features

* **#5:** resolve Vault bindings locally ([bcbb7b9](https://github.com/lightnow-ai/lightnow-proxy/commit/bcbb7b9e330fc73af861937b2e3ccc3a8fb01687))
* report proxy health failure details ([d078020](https://github.com/lightnow-ai/lightnow-proxy/commit/d078020416abeecbf470f4ee41dd798fcf76c97b))
* **secrets:** resolve Vault bindings locally ([1e78135](https://github.com/lightnow-ai/lightnow-proxy/commit/1e78135ecb1b889f76507eca878b30b5fa3fdcad))


### Bug Fixes

* default local proxy health config ([7e86405](https://github.com/lightnow-ai/lightnow-proxy/commit/7e8640557ff4e54f6eb09fe11dab84685c48ea0d))

## [1.2.0](https://github.com/lightnow-ai/lightnow-proxy/compare/v1.1.0...v1.2.0) (2026-07-06)


### Features

* **#1:** Prepare official MCP registry listing ([c32c16e](https://github.com/lightnow-ai/lightnow-proxy/commit/c32c16e7ef7a85baf5fa3eeca52c82fd8e2ad017))

## Changelog

All notable changes to the LightNow Local Proxy are documented here.

This project follows semantic versioning.
