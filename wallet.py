"""Signs and sends transactions with a Solana keypair exported from Phantom."""
import base64
import json
import time

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction


class Wallet:
    def __init__(self, rpc_url, private_key):
        if not private_key:
            raise ValueError("SOLANA_PRIVATE_KEY is empty")
        if private_key.startswith("["):
            self.keypair = Keypair.from_bytes(bytes(json.loads(private_key)))
        else:
            self.keypair = Keypair.from_base58_string(private_key)
        self.pubkey = self.keypair.pubkey()
        self.rpc_url = rpc_url

    def _rpc(self, method, params):
        r = requests.post(self.rpc_url, timeout=20, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"RPC {method} error: {data['error']}")
        return data["result"]

    def sol_balance(self):
        res = self._rpc("getBalance", [str(self.pubkey), {"commitment": "confirmed"}])
        return res["value"] / 1e9

    def token_balance(self, mint):
        res = self._rpc("getTokenAccountsByOwner", [
            str(self.pubkey), {"mint": mint},
            {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return sum(float(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                   for acc in res["value"])

    def sign_and_send(self, tx_b64):
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        encoded = base64.b64encode(bytes(signed)).decode()
        return self._rpc("sendTransaction", [encoded, {
            "encoding": "base64", "skipPreflight": False,
            "preflightCommitment": "confirmed", "maxRetries": 3}])

    def confirm(self, signature, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self._rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            status = res["value"][0]
            if status:
                if status.get("err"):
                    raise RuntimeError(f"Transaction failed on-chain: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            time.sleep(2)
        raise TimeoutError(f"Transaction {signature} not confirmed in {timeout}s — check Solscan")
