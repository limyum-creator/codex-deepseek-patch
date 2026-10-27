# Linux CLI builds from the maintained fork

This fork's `local/linux-glm` branch builds adapters for the installed official
`@openai/codex` npm CLI on Linux x86_64 and aarch64. Build stages configuration
and runtime files only. It does not replace Codex, edit authentication, start
services, or apply the prepared configuration.

Linux identity checks validate npm package metadata, the ELF executable, its
reported version, and its SHA-256. This records the installed binary's identity;
it does not claim macOS or Windows publisher-signature verification.

## Build

Use the repository entrypoint from a clean, committed checkout. Keep connection
URLs, key-file references, and model preferences outside this public repository.
For example, with a private `connections.json` created by `patch.py connect`:

```sh
python3 patch.py build \
  --app /path/to/node_modules/@openai/codex \
  --codex-home /path/to/codex-home \
  --connections /private/connections.json \
  --model-preferences /private/model-preferences.json \
  --compression-node /path/to/node \
  --without-deepseek \
  --output /private/new-adapter-build \
  --verify
```

`--app` defaults to the npm package containing the `codex` command on `PATH`.
On Python below 3.14, use an existing Node runtime whose `node:zlib` provides
`zstdCompressSync` and `zstdDecompressSync`; the builder checks a round trip.
Node 24 supports these APIs. An older Node may run the Codex npm launcher but
lack this codec. Python 3.14 uses standard-library zstd and needs no Node codec.

When rebuilding an existing integration, add `--baseline /private/current-build`.
The baseline proves ownership of existing managed configuration and retains the
listener address. Supplying `--connections` rediscovers the explicitly saved
services; supplying `--model-preferences` applies per-model limits even when a
native baseline already exists. Omit `--connections` to retain baseline routes.
Use a new output directory for each build. Do not overwrite a running build.

For a model whose maximum is 1,048,576 tokens, declare that maximum in its private
connection's `model_specs` and select the desired working window separately:

```json
{
  "context_windows": {"your-model-id": 524288}
}
```

External models compact at 90% of their selected window by default, producing
`auto_compact_token_limit: 471859` for this example. An explicit `compact_limits`
entry may select a different threshold. Rebuilding regenerates both the model
catalog and adapter profile from these inputs.

`manifest.json` records the source revision and dirty state when built from a Git
checkout, hashes every deployed adapter file, and records the installed Codex
binary hash. `--verify` uses the repository's disposable native CLI regression
with synthetic authentication and loopback endpoints. A passing fixture suite
is separate evidence from successful real-provider inference and compaction.

## Deployment boundary

The existing macOS and Windows service installer is not a Linux service manager.
On Linux, review `config-prepared.toml`, then use the existing local deployment
mechanism to activate a validated build at an idle boundary. Keep the previous
build, previous configuration, and old router available for rollback. Do not
copy individual source files over a running adapter: deploy the complete build
whose manifest hashes were verified.
