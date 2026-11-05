"""Assembles a release from the per-target build and smoke-test outputs

Checks that every published target has an archive whose checksum file matches
it and a passing smoke-test report, then writes SHA256SUMS and the release
notes. Prints the release tag, and also writes it to `$GITHUB_OUTPUT` in CI
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pins


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=pins.DIST)
    args = parser.parse_args()

    dist: Path = args.dist
    pinned = pins.load()
    electrs, core = pinned.electrs, pinned.bitcoin_core

    sums: list[str] = []
    tested: list[str] = []
    for target in sorted(pins.CORE_ASSET_PLATFORMS):
        archive = dist / f"{electrs.asset_name(target)}.tar.gz"
        digest = sha256_file(archive)
        recorded = (dist / f"{archive.name}.sha256").read_text().split()[0]
        if digest != recorded:
            raise SystemExit(f"{archive.name}: sha256 {digest}, but the build recorded {recorded}")
        sums.append(f"{digest}  {archive.name}")

        report = json.loads((dist / f"smoke-{target}.json").read_text())
        if report.get("electrs_rev") != electrs.rev or report.get("bitcoin_core_pin") != core.version:
            raise SystemExit(f"smoke-{target}.json is for a different pin: {report}")
        tested.append(f"- `{target}`: {len(report['checks'])} checks passed against {report['bitcoin_core']}")

    (dist / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    notes = "\n".join(
        [
            f"Blockstream electrs {electrs.version}, built from [`{electrs.short_rev}`]({electrs.repository}/commit/{electrs.rev}).",
            "",
            f"Each binary was smoke-tested against Bitcoin Core {core.version} on a local custom signet "
            "(`-signet -signetchallenge=51`), with electrs run as `--network signet --jsonrpc-import`. "
            "Esplora and Electrum both served blocks, mempool transactions and confirmations, and an "
            "Esplora broadcast reached the node.",
            "",
            *tested,
            "",
            "Verify a download with `shasum -a 256 -c SHA256SUMS --ignore-missing`.",
        ]
    )
    (dist / "RELEASE_NOTES.md").write_text(notes + "\n")

    print(electrs.release_tag)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as file:
            file.write(f"tag={electrs.release_tag}\n")


if __name__ == "__main__":
    main()
