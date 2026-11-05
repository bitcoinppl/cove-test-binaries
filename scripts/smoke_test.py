"""Runs an electrs binary against the pinned Bitcoin Core on a local custom signet

This is the setup Cove's tests use: `bitcoind -signet -signetchallenge=51`, which
keeps signet's genesis block, and electrs with `--jsonrpc-import`, since the
custom challenge changes the network magic electrs would need to read block
files. The test checks that both Esplora and Electrum serve blocks, mempool
transactions and confirmations, and that an Esplora broadcast reaches the node
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pins

SIGNET_GENESIS = "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6"

# signet blocks need real proof of work, about 4.8M hashes on average, and
# bitcoind's default of 1M tries per call usually gives up before finding one
MAX_TRIES = 1_000_000_000

# coinbase outputs mature after 100 blocks, so the wallet can spend one
MATURE_HEIGHT = 101

T = TypeVar("T")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def wait_for(description: str, check: Callable[[], T | None], timeout: float = 60.0, poke: Callable[[], None] | None = None) -> T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if poke is not None:
            poke()
        try:
            value = check()
            if value is not None:
                return value
        except (OSError, RpcError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {description}: {last_error}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_bitcoind(core: pins.CorePin, target: str, cache: Path) -> Path:
    """Downloads the pinned Core release once, checks its SHA-256 and extracts bitcoind"""
    tarball = cache / core.tarball_name(target)
    expected = core.sha256[target]
    if not tarball.exists() or sha256_file(tarball) != expected:
        cache.mkdir(parents=True, exist_ok=True)
        partial = tarball.with_suffix(".partial")
        print(f"downloading {core.tarball_url(target)}", flush=True)
        with urllib.request.urlopen(core.tarball_url(target), timeout=120) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        actual = sha256_file(partial)
        if actual != expected:
            partial.unlink()
            raise SystemExit(f"{tarball.name}: sha256 {actual}, expected {expected}")
        partial.rename(tarball)

    extracted = cache / f"bitcoin-{core.version}-{target}"
    bitcoind = extracted / f"bitcoin-{core.version}" / "bin" / "bitcoind"
    if not bitcoind.exists():
        shutil.rmtree(extracted, ignore_errors=True)
        with tarfile.open(tarball) as tar:
            tar.extractall(extracted, filter="data")

    # macOS refuses to run an arm64 binary with no valid signature, so sign it ad hoc if needed
    if target.endswith("apple-darwin"):
        verified = subprocess.run(["codesign", "--verify", str(bitcoind)], capture_output=True)
        if verified.returncode != 0:
            subprocess.run(["codesign", "--force", "--sign", "-", str(bitcoind)], check=True)

    return bitcoind


def print_log_tail(name: str, path: Path) -> None:
    print(f"--- {name} log (last 40 lines) ---")
    print("\n".join(path.read_text().splitlines()[-40:]), flush=True)


class RpcError(Exception):
    pass


class Bitcoind:
    def __init__(self, binary: Path, datadir: Path) -> None:
        self.datadir = datadir
        datadir.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.log_path = datadir / "bitcoind.out"
        self.log = self.log_path.open("w")
        self.process = subprocess.Popen(
            [
                str(binary),
                f"-datadir={datadir}",
                "-signet",
                "-signetchallenge=51",
                "-listen=0",
                "-server=1",
                f"-rpcport={self.port}",
                "-rpcbind=127.0.0.1",
                "-rpcallowip=127.0.0.1",
                "-fallbackfee=0.0002",
                "-printtoconsole=0",
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        # the custom challenge still uses the signet subdirectory on Core 32.1
        self.cookie_path = datadir / "signet" / ".cookie"

        # main() only holds the instance once this returns, so a failed start cleans up here
        try:
            wait_for("the bitcoind cookie", lambda: self.cookie_path.exists() or None)
            wait_for("bitcoind RPC", lambda: self.call("getblockchaininfo"))
        except BaseException:
            self.stop()
            print_log_tail("bitcoind", self.log_path)
            raise

    @property
    def cookie(self) -> str:
        return self.cookie_path.read_text().strip()

    def call(self, method: str, *params: object, wallet: str | None = None) -> object:
        if self.process.poll() is not None:
            raise SystemExit(f"bitcoind exited with {self.process.returncode}")
        path = f"/wallet/{wallet}" if wallet else "/"
        body = json.dumps({"jsonrpc": "1.0", "id": method, "method": method, "params": list(params)}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        request.add_header("Authorization", "Basic " + base64.b64encode(self.cookie.encode()).decode())
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                reply = json.load(response)
        except urllib.error.HTTPError as error:
            reply = json.load(error)
        if reply.get("error"):
            raise RpcError(f"{method}: {reply['error']}")
        result: object = reply["result"]
        return result

    def mine(self, blocks: int, address: str) -> None:
        hashes = self.call("generatetoaddress", blocks, address, MAX_TRIES)
        if not isinstance(hashes, list) or len(hashes) != blocks:
            raise SystemExit(f"generatetoaddress mined {hashes!r}, expected {blocks} blocks")

    def stop(self) -> None:
        stop_process(self.process)
        self.log.close()


class Electrs:
    def __init__(self, binary: Path, bitcoind: Bitcoind, workdir: Path) -> None:
        self.electrum_port = free_port()
        self.http_port = free_port()
        self.log_path = workdir / "electrs.log"
        self.log = self.log_path.open("w")
        self.process = subprocess.Popen(
            [
                str(binary),
                "-vv",
                "--network",
                "signet",
                "--jsonrpc-import",
                "--daemon-dir",
                str(bitcoind.datadir),
                "--cookie",
                bitcoind.cookie,
                "--db-dir",
                str(workdir / "electrs-db"),
                "--daemon-rpc-addr",
                f"127.0.0.1:{bitcoind.port}",
                "--electrum-rpc-addr",
                f"127.0.0.1:{self.electrum_port}",
                "--http-addr",
                f"127.0.0.1:{self.http_port}",
                "--monitoring-addr",
                f"127.0.0.1:{free_port()}",
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

        # SIGUSR1 kills electrs until it installs its handler, which it has done by the
        # time its HTTP server answers, so pokes wait for that
        self.ready = False
        try:
            wait_for("electrs to answer HTTP", lambda: self.esplora("/blocks/tip/hash"))
        except BaseException:
            self.stop()
            print_log_tail("electrs", self.log_path)
            raise
        self.ready = True

    def poke(self) -> None:
        """Wakes electrs to sync now instead of at its next 5-second poll"""
        if self.process.poll() is not None:
            raise SystemExit(f"electrs exited with {self.process.returncode}")
        if self.ready:
            self.process.send_signal(signal.SIGUSR1)

    def esplora(self, path: str, body: str | None = None) -> str | None:
        """The response body, or None for a 404"""
        if self.process.poll() is not None:
            raise SystemExit(f"electrs exited with {self.process.returncode}")
        data = body.encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.http_port}{path}", data=data)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                text: str = response.read().decode()
                return text
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise RpcError(f"esplora {path}: {error.code} {error.read().decode()}") from error

    def electrum(self, method: str, *params: object) -> object:
        with socket.create_connection(("127.0.0.1", self.electrum_port), timeout=10) as connection:
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)})
            connection.sendall(request.encode() + b"\n")
            reply = json.loads(connection.makefile().readline())
        if reply.get("error"):
            raise RpcError(f"electrum {method}: {reply['error']}")
        result: object = reply["result"]
        return result

    def stop(self) -> None:
        stop_process(self.process)
        self.log.close()


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def scripthash(bitcoind: Bitcoind, address: str) -> str:
    """Electrum's script hash: SHA-256 of the output script, byte-reversed"""
    info = bitcoind.call("getaddressinfo", address, wallet="miner")
    if not isinstance(info, dict):
        raise SystemExit(f"getaddressinfo returned {info!r}")
    script = bytes.fromhex(str(info["scriptPubKey"]))
    return hashlib.sha256(script).digest()[::-1].hex()


def history_height(electrs: Electrs, hash: str, txid: str) -> int | None:
    history = electrs.electrum("blockchain.scripthash.get_history", hash)
    if not isinstance(history, list):
        raise SystemExit(f"get_history returned {history!r}")
    for entry in history:
        if isinstance(entry, dict) and entry.get("tx_hash") == txid:
            height = entry.get("height")
            return height if isinstance(height, int) else None
    return None


def tx_status(electrs: Electrs, txid: str) -> dict[str, object] | None:
    text = electrs.esplora(f"/tx/{txid}/status")
    if text is None:
        return None
    status = json.loads(text)
    if not isinstance(status, dict):
        raise SystemExit(f"/tx/{txid}/status returned {text}")
    return {str(key): value for key, value in status.items()}


def run_checks(bitcoind: Bitcoind, electrs: Electrs, checks: list[str]) -> None:
    def passed(check: str) -> None:
        print(f"ok: {check}", flush=True)
        checks.append(check)

    tip = str(MATURE_HEIGHT)
    wait_for("Esplora to reach the pre-mined tip", lambda: electrs.esplora("/blocks/tip/height") == tip or None, poke=electrs.poke)
    passed(f"Esplora serves the tip at height {tip}")

    genesis = electrs.esplora("/block-height/0")
    if genesis != SIGNET_GENESIS:
        raise SystemExit(f"genesis is {genesis}, expected signet's {SIGNET_GENESIS}")
    passed("the chain starts from signet's genesis block")

    header = electrs.electrum("blockchain.headers.subscribe")
    if not isinstance(header, dict) or header.get("height") != MATURE_HEIGHT:
        raise SystemExit(f"Electrum tip is {header!r}")
    passed(f"Electrum serves the tip at height {tip}")

    # a wallet send, which electrs must serve while it is still in the mempool
    destination = str(bitcoind.call("getnewaddress", wallet="miner"))
    txid = str(bitcoind.call("sendtoaddress", destination, 1.0, wallet="miner"))
    status = wait_for("Esplora to serve the mempool transaction", lambda: tx_status(electrs, txid), poke=electrs.poke)
    if status.get("confirmed") is not False:
        raise SystemExit(f"mempool transaction status is {status}")
    passed("Esplora serves a mempool transaction")

    hash = scripthash(bitcoind, destination)
    wait_for("Electrum history to show the mempool transaction", lambda: history_height(electrs, hash, txid) == 0 or None, poke=electrs.poke)
    passed("Electrum history shows a mempool transaction")

    # a broadcast through Esplora must reach the node's mempool
    raw = bitcoind.call("createrawtransaction", [], {str(bitcoind.call("getnewaddress", wallet="miner")): 0.5})
    funded = bitcoind.call("fundrawtransaction", raw, wallet="miner")
    signed = bitcoind.call("signrawtransactionwithwallet", funded["hex"] if isinstance(funded, dict) else None, wallet="miner")
    if not isinstance(signed, dict) or signed.get("complete") is not True:
        raise SystemExit(f"signrawtransactionwithwallet returned {signed!r}")
    broadcast = electrs.esplora("/tx", str(signed["hex"]))
    if broadcast is None:
        raise SystemExit("Esplora POST /tx returned 404")
    bitcoind.call("getmempoolentry", broadcast.strip())
    passed("an Esplora broadcast reaches the node's mempool")

    # a block confirms both, on both protocols
    bitcoind.mine(1, destination)
    confirmed_height = MATURE_HEIGHT + 1
    wait_for(
        "Esplora to confirm the transaction",
        lambda: (tx_status(electrs, txid) or {}).get("block_height") == confirmed_height or None,
        poke=electrs.poke,
    )
    passed(f"Esplora reports the confirmation at height {confirmed_height}")

    wait_for(
        "Electrum history to confirm the transaction",
        lambda: history_height(electrs, hash, txid) == confirmed_height or None,
        poke=electrs.poke,
    )
    passed(f"Electrum history reports the confirmation at height {confirmed_height}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--electrs", type=Path, required=True, help="the electrs binary to test")
    parser.add_argument("--bitcoind", type=Path, help="use this bitcoind instead of the pinned download")
    parser.add_argument("--target", default=pins.host_target(), choices=sorted(pins.CORE_ASSET_PLATFORMS))
    parser.add_argument("--cache", type=Path, default=pins.WORK / "cache")
    parser.add_argument("--report", type=Path, help="write a JSON report of the run here")
    args = parser.parse_args()

    pinned = pins.load()
    bitcoind_binary: Path = args.bitcoind or fetch_bitcoind(pinned.bitcoin_core, args.target, args.cache)
    core_version = subprocess.run([str(bitcoind_binary), "-version"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
    electrs_version = subprocess.run([str(args.electrs), "--version"], check=True, capture_output=True, text=True).stdout.strip()
    print(f"{core_version}\n{electrs_version}", flush=True)

    checks: list[str] = []
    timings: dict[str, float] = {}
    workdir = Path(tempfile.mkdtemp(prefix="electrs-smoke-"))
    bitcoind: Bitcoind | None = None
    electrs: Electrs | None = None
    succeeded = False
    try:
        bitcoind = Bitcoind(bitcoind_binary, workdir / "bitcoind")
        bitcoind.call("createwallet", "miner")
        miner = str(bitcoind.call("getnewaddress", wallet="miner"))

        # mine before electrs starts: it waits for bitcoind to leave initial block download
        started = time.monotonic()
        bitcoind.mine(MATURE_HEIGHT, miner)
        timings["premine_seconds"] = round(time.monotonic() - started, 1)
        print(f"mined {MATURE_HEIGHT} blocks in {timings['premine_seconds']} s", flush=True)

        electrs = Electrs(args.electrs, bitcoind, workdir)
        started = time.monotonic()
        run_checks(bitcoind, electrs, checks)
        timings["checks_seconds"] = round(time.monotonic() - started, 1)
        succeeded = True
    finally:
        for service in (electrs, bitcoind):
            if service is not None:
                service.stop()
        if not succeeded and electrs is not None:
            print_log_tail("electrs", electrs.log_path)
        if succeeded or not os.environ.get("CI"):
            shutil.rmtree(workdir, ignore_errors=True)

    if args.report:
        report = {
            "target": args.target,
            "bitcoin_core": core_version,
            "bitcoin_core_pin": pinned.bitcoin_core.version,
            "electrs": electrs_version,
            "electrs_rev": pinned.electrs.rev,
            "checks": checks,
            **timings,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"all {len(checks)} checks passed")


if __name__ == "__main__":
    main()
