# cove-test-binaries

Prebuilt, checksummed binaries for [Cove](https://github.com/bitcoinppl/cove)'s test infrastructure. These are for tests only and are never shipped in the app.

The first is Blockstream's [electrs](https://github.com/Blockstream/electrs), the Esplora and Electrum server behind Cove's local custom signet ([bitcoinppl/cove#1034](https://github.com/bitcoinppl/cove/issues/1034)). Blockstream publishes no release binaries for it, and building it from source takes several minutes, mostly in RocksDB.

## Releases

Each release is built by this repo's GitHub Actions from a pinned upstream revision. It is published with a `SHA256SUMS` file and records the Bitcoin Core version it was tested against. Cove pins each binary by version and checksum, so a new release here changes nothing until Cove updates its pin.

Targets:

- `x86_64-unknown-linux-gnu`
- `aarch64-apple-darwin`

## How it works

`pins.json` pins everything: the electrs repository, its revision and version, and the Bitcoin Core version used for testing, with each Core tarball's SHA-256.

The `Build` workflow runs on pull requests, on pushes to `master` and by hand. For each target it runs three scripts:

1. **`scripts/build_electrs.py`** fetches the pinned commit and builds it with upstream's `rust-toolchain.toml`. It checks that the binary reports the pinned version, then packages `electrs`, upstream's `LICENSE` and a `BUILD.json` into `electrs-v<version>-<rev>-<target>.tar.gz`, along with a `.sha256` file.
2. **`scripts/smoke_test.py`** downloads the pinned Bitcoin Core, verifies its checksum and starts a custom signet (`-signet -signetchallenge=51`). It pre-mines 101 blocks and runs electrs with `--network signet --jsonrpc-import`. It then checks the following over Esplora and Electrum:
   - the tip and signet's genesis block;
   - a mempool transaction;
   - an Esplora broadcast reaching the node;
   - the confirmation.
3. **`scripts/release.py`** runs only on `master`. It checks every target's archive and smoke report, then writes `SHA256SUMS` and the release notes. The workflow publishes them as release `electrs-v<version>-<rev>`, unless that tag already exists.

To release a new electrs or Core version, change `pins.json` in a pull request. Merging it publishes the new tag. Published releases are never replaced.

The scripts are typed Python with no dependencies outside the standard library, checked with `mypy --strict`. Locally:

```sh
uv run --python 3.13 scripts/build_electrs.py
uv run --python 3.13 scripts/smoke_test.py --electrs .work/electrs/target/release/electrs
uvx --python 3.13 mypy@2.4.0
```

The Linux binary is built on Ubuntu 24.04 and needs glibc 2.39 or newer.

## License

The build scripts in this repo are MIT licensed. Each binary keeps its upstream license, and electrs is MIT.
