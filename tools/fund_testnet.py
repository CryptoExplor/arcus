#!/usr/bin/env python3
"""Fund a testnet account on-chain: mint -> approve -> initiateDeposit.

Most users should just click **Testnet Deposit** in the web app (~$1,000 USDG).
Use this when you need a bigger or programmatic balance.

    pip install -r requirements-onboard.txt
    python tools/fund_testnet.py --private-key 0x... --amount 5000

WARNING — the contract ADDRESSES below change on every testnet redeploy or
network reset. The FLOW (mint -> approve -> initiateDeposit -> wait) is stable;
the values are not. A stale address fails one of two ways: an obvious
`WrongToken`/transfer revert, or a transaction that succeeds on-chain but never
credits your account. Verify against the current docs before relying on this:
https://docs.arcus.xyz/guides/fund-testnet-account

Your wallet also needs a little RH-testnet ETH for gas (~0.001). There is no
public faucet — transfer from an already-funded wallet, or ask the Arcus team.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from arcusbot.config import (  # noqa: E402
    DEPOSIT_PROXY_TESTNET,
    RH_TESTNET_CHAIN_ID,
    RH_TESTNET_RPC,
    TESTNET_REST,
    USDG_TESTNET,
)

USDG_DECIMALS = 6  # $1,000 == 1_000_000_000

ERC20_ABI = [
    {"name": "mint", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": []},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
]

PROXY_ABI = [
    {"name": "initiateDeposit", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "owner", "type": "address"},
         {"name": "accountIndex", "type": "uint16"},
         {"name": "token", "type": "address"},
         {"name": "amount", "type": "uint256"},
     ], "outputs": []},
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Deposit testnet USDG collateral")
    ap.add_argument("--private-key", required=True)
    ap.add_argument("--amount", type=str, default="1000", help="USD amount, e.g. 5000")
    ap.add_argument("--account-index", type=int, default=0)
    ap.add_argument("--rpc", default=RH_TESTNET_RPC)
    ap.add_argument("--usdg", default=USDG_TESTNET)
    ap.add_argument("--proxy", default=DEPOSIT_PROXY_TESTNET)
    ap.add_argument("--skip-mint", action="store_true", help="already hold USDG")
    args = ap.parse_args()

    try:
        from web3 import Web3
    except ImportError:
        raise SystemExit("missing deps — run: pip install -r requirements-onboard.txt")

    amount = int(Decimal(args.amount) * (10 ** USDG_DECIMALS))
    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        raise SystemExit(f"cannot reach RPC {args.rpc}")
    chain_id = w3.eth.chain_id
    if chain_id != RH_TESTNET_CHAIN_ID:
        print(f"WARNING: chain id {chain_id} != expected {RH_TESTNET_CHAIN_ID}")

    acct = w3.eth.account.from_key(args.private_key)
    gas_balance = w3.eth.get_balance(acct.address)
    print(f"wallet {acct.address}  gas {w3.from_wei(gas_balance, 'ether')} ETH")
    if gas_balance == 0:
        raise SystemExit("no RH-testnet ETH for gas — seed the wallet first (no public faucet)")

    usdg = w3.eth.contract(address=Web3.to_checksum_address(args.usdg), abi=ERC20_ABI)
    proxy = w3.eth.contract(address=Web3.to_checksum_address(args.proxy), abi=PROXY_ABI)

    def send(fn, label: str, retries: int = 2):
        for attempt in range(retries + 1):
            try:
                tx = fn.build_transaction({
                    "from": acct.address,
                    "nonce": w3.eth.get_transaction_count(acct.address),
                    "chainId": chain_id,
                })
                # Never hard-code 21,000 — the chain rejects it as "intrinsic gas too low".
                signed = acct.sign_transaction(tx)
                raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
                tx_hash = w3.eth.send_raw_transaction(raw)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
                if receipt.status != 1:
                    raise RuntimeError(f"reverted: {receipt.transactionHash.hex()}")
                print(f"  {label}: {receipt.transactionHash.hex()}")
                return receipt
            except Exception as exc:
                if attempt >= retries:
                    raise SystemExit(f"{label} failed: {exc}")
                print(f"  {label} failed ({exc}); retrying…")
                time.sleep(3)

    print(f"depositing ${args.amount} USDG to accountIndex {args.account_index}")
    if not args.skip_mint:
        send(usdg.functions.mint(acct.address, amount), "1/3 mint")
    # The spender is the deposit PROXY, not the bridge.
    send(usdg.functions.approve(proxy.address, amount), "2/3 approve")
    allowance = usdg.functions.allowance(acct.address, proxy.address).call()
    if allowance < amount:
        raise SystemExit(f"allowance {allowance} < {amount} — approve did not stick")
    send(proxy.functions.initiateDeposit(acct.address, args.account_index, usdg.address, amount),
         "3/3 initiateDeposit")

    print("waiting for the exchange to credit the deposit", end="", flush=True)
    url = f"{TESTNET_REST}/v1/account?address={acct.address}&accountIndex={args.account_index}"
    for _ in range(60):
        time.sleep(3)
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                account = json.loads(resp.read() or b"{}")
            equity = Decimal(str(account.get("equity", "0") or "0"))
            if equity > 0:
                print(f"\ncredited — equity ${equity}, free ${account.get('freeCollateral')}")
                return 0
        except Exception:
            pass  # 404 until the first event lands
        print(".", end="", flush=True)

    print("\nnot credited yet. The tx may still be settling; check:")
    print(f"  curl '{url}'")
    print(f"  curl '{TESTNET_REST}/v1/accountTransferUpdates?address={acct.address}&limit=50'")
    print("If it never lands, the deposit observer may be behind (common after a testnet")
    print("reset) or the contract addresses are stale — verify them against the docs.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
