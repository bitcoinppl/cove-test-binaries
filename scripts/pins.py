"""Typed access to pins.json, the single source of every pinned version and checksum"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".work"
DIST = ROOT / "dist"

# targets this repo publishes, mapped to the name bitcoincore.org gives the same platform
CORE_ASSET_PLATFORMS: dict[str, str] = {
    "x86_64-unknown-linux-gnu": "x86_64-linux-gnu",
    "aarch64-apple-darwin": "arm64-apple-darwin",
}


@dataclass(frozen=True)
class ElectrsPin:
    repository: str
    rev: str
    version: str

    @property
    def short_rev(self) -> str:
        return self.rev[:7]

    @property
    def release_tag(self) -> str:
        return f"electrs-v{self.version}-{self.short_rev}"

    def asset_name(self, target: str) -> str:
        return f"{self.release_tag}-{target}"


@dataclass(frozen=True)
class CorePin:
    version: str
    sha256: dict[str, str]

    def tarball_name(self, target: str) -> str:
        return f"bitcoin-{self.version}-{CORE_ASSET_PLATFORMS[target]}.tar.gz"

    def tarball_url(self, target: str) -> str:
        return f"https://bitcoincore.org/bin/bitcoin-core-{self.version}/{self.tarball_name(target)}"


@dataclass(frozen=True)
class Pins:
    electrs: ElectrsPin
    bitcoin_core: CorePin


def _str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"pins.json: {field} must be a non-empty string")
    return value


def _dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"pins.json: {field} must be an object")
    return {str(key): item for key, item in value.items()}


def load() -> Pins:
    raw = _dict(json.loads((ROOT / "pins.json").read_text()), "root")
    electrs = _dict(raw.get("electrs"), "electrs")
    core = _dict(raw.get("bitcoin_core"), "bitcoin_core")
    sums = _dict(core.get("sha256"), "bitcoin_core.sha256")

    rev = _str(electrs.get("rev"), "electrs.rev")
    if len(rev) != 40:
        raise ValueError("pins.json: electrs.rev must be a full 40-character commit")

    unknown = set(sums) - set(CORE_ASSET_PLATFORMS)
    if unknown:
        raise ValueError(f"pins.json: unknown targets in bitcoin_core.sha256: {sorted(unknown)}")

    return Pins(
        electrs=ElectrsPin(
            repository=_str(electrs.get("repository"), "electrs.repository"),
            rev=rev,
            version=_str(electrs.get("version"), "electrs.version"),
        ),
        bitcoin_core=CorePin(
            version=_str(core.get("version"), "bitcoin_core.version"),
            sha256={target: _str(digest, f"bitcoin_core.sha256.{target}") for target, digest in sums.items()},
        ),
    )


def host_target() -> str:
    """The published target this machine builds natively, or an error for any other host"""
    system, machine = platform.system(), platform.machine().lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "x86_64-unknown-linux-gnu"
    if system == "Darwin" and machine == "arm64":
        return "aarch64-apple-darwin"
    raise SystemExit(f"no published target for {system} {machine}")
