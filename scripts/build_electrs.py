"""Builds Blockstream electrs at the pinned revision and packages it for release

The build is native: the target must be the host's, since electrs links RocksDB
through a C++ build that this repo doesn't cross-compile. The archive holds the
`electrs` binary, upstream's LICENSE and a BUILD.json describing the build
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pins


def run(args: list[str], cwd: Path | None = None) -> str:
    print("+", " ".join(args), flush=True)
    result = subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, text=True)
    return result.stdout.strip()


def checkout(pin: pins.ElectrsPin, source: Path) -> None:
    """Fetches only the pinned commit, reusing an existing checkout"""
    if not (source / ".git").exists():
        source.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q"], cwd=source)
        run(["git", "remote", "add", "origin", pin.repository], cwd=source)

    run(["git", "fetch", "-q", "--depth", "1", "origin", pin.rev], cwd=source)
    run(["git", "checkout", "-q", "--force", "FETCH_HEAD"], cwd=source)

    head = run(["git", "rev-parse", "HEAD"], cwd=source)
    if head != pin.rev:
        raise SystemExit(f"checked out {head}, expected {pin.rev}")


def rustc_info(source: Path) -> dict[str, str]:
    """`rustc -vV` from inside the checkout, so it reports upstream's rust-toolchain.toml"""
    lines = run(["rustc", "-vV"], cwd=source).splitlines()
    info = {"version": lines[0]}
    for line in lines[1:]:
        key, _, value = line.partition(": ")
        info[key] = value
    return info


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package(pin: pins.ElectrsPin, target: str, binary: Path, source: Path, rustc: dict[str, str], dist: Path) -> Path:
    name = pin.asset_name(target)
    stage = pins.WORK / "stage" / name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)

    shutil.copy2(binary, stage / "electrs")
    shutil.copy2(source / "LICENSE", stage / "LICENSE")
    build = {
        "repository": pin.repository,
        "rev": pin.rev,
        "version": pin.version,
        "target": target,
        "rustc": rustc["version"],
    }
    (stage / "BUILD.json").write_text(json.dumps(build, indent=2) + "\n")

    dist.mkdir(parents=True, exist_ok=True)
    archive = dist / f"{name}.tar.gz"

    # fixed ownership and mode so the archive doesn't carry the runner's user
    def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode = 0o755 if info.isdir() or info.name.endswith("/electrs") else 0o644
        return info

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=name, filter=normalize)

    (dist / f"{archive.name}.sha256").write_text(f"{sha256_file(archive)}  {archive.name}\n")
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=pins.host_target(), choices=sorted(pins.CORE_ASSET_PLATFORMS))
    parser.add_argument("--dist", type=Path, default=pins.DIST)
    args = parser.parse_args()

    target: str = args.target
    if target != pins.host_target():
        raise SystemExit(f"{target} must be built on a {target} host")

    pin = pins.load().electrs
    source = pins.WORK / "electrs"
    checkout(pin, source)

    # rustup installs the toolchain upstream's rust-toolchain.toml names, if it's missing
    if shutil.which("rustup"):
        run(["rustup", "toolchain", "install"], cwd=source)

    rustc = rustc_info(source)
    if rustc.get("host") != target:
        raise SystemExit(f"rustc host is {rustc.get('host')}, expected {target}")

    run(["cargo", "build", "--release", "--locked", "--bin", "electrs"], cwd=source)
    binary = source / "target" / "release" / "electrs"

    # the binary must at least start and report the pinned version
    reported = run([str(binary), "--version"])
    if pin.version not in reported:
        raise SystemExit(f"electrs reports {reported!r}, expected version {pin.version}")

    archive = package(pin, target, binary, source, rustc, args.dist)
    print(f"built {archive}")


if __name__ == "__main__":
    main()
