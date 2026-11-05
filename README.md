# cove-test-binaries

Prebuilt, checksummed binaries for [Cove](https://github.com/bitcoinppl/cove)'s test infrastructure. These are for tests only and are never shipped in the app.

The first is Blockstream's [electrs](https://github.com/Blockstream/electrs), the Esplora and Electrum server behind Cove's local custom signet ([bitcoinppl/cove#1034](https://github.com/bitcoinppl/cove/issues/1034)). Blockstream publishes no release binaries for it, and building it from source takes several minutes, mostly in RocksDB.

## Releases

Each release is built by this repo's GitHub Actions from a pinned upstream revision. It is published with a `SHA256SUMS` file and records the Bitcoin Core version it was tested against. Cove pins each binary by version and checksum, so a new release here changes nothing until Cove updates its pin.

Targets:

- `x86_64-unknown-linux-gnu`
- `aarch64-apple-darwin`

## License

The build scripts in this repo are MIT licensed. Each binary keeps its upstream license, and electrs is MIT.
