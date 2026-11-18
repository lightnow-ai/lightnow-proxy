# Client source gates and release reuse

CLI and Proxy remain native FeatureStack `clientArtifacts`; their PR wheel
manifest, Actions run/attempt, exact head/checkout revisions and wheel digest
remain unchanged. FeatureStack baseline integration and Kargo native acceptance
continue to consume those immutable tested distributions.

Every selected Python version executes the entire source suite. Documentation
changes select Python 3.12, ordinary Python changes select 3.11 and 3.12, and
security/configuration/runtime/dependency/build/CI changes select all three
supported versions. Unknown paths and main builds select all three. This reduces
compatibility duplication without weakening the principal source suite.

Pytest's public collection hook records native node IDs in independent JUnit.
Collection and execution must match exactly; the current reviewed skip baseline
is empty. Native Allure 3.33 gates zero failed/broken cases and requires the
entire discovered required count to pass. Raw pytest errors also block package
publication. Original results use Allure's native ZIP packer; report HTML is
compressed once and uploaded with collection and JUnit evidence.

A tag release waits for successful main CI at exactly the tag commit, downloads
that run's checked wheel and sdist, checks tag/package identity and forwards the
same bytes to GitHub and PyPI. There is no tag test/build fallback. Expired or
missing source artifacts require an explicit source CI rerun. Keep successful
main artifacts retained until release publication; this uses the repository's
Actions artifact retention policy. PR artifacts retain their existing schema.

Proxy Jenkins separately builds its actual runtime Docker image and executes
its installed entry point smoke. Source pytest belongs to Actions; it previously
copied source into the runtime image and installed a separate dev environment,
so that duplicate suite did not validate the installed image distribution.
