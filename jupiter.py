"""Minimal Jupiter Swap API client (quote -> build transaction)."""
import requests
import config as C


class Jupiter:
    def __init__(self, base_url, api_key=""):
        self.base = base_url
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["x-api-key"] = api_key

    def quote(self, input_mint, output_mint, amount_raw, slippage_bps):
        r = requests.get(f"{self.base}/quote", headers=self.headers, timeout=15, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_raw)),
            "slippageBps": slippage_bps,
            "restrictIntermediateTokens": "true",
        })
        r.raise_for_status()
        q = r.json()
        if "outAmount" not in q:
            raise RuntimeError(f"Bad Jupiter quote: {q}")
        return q

    def swap_transaction(self, quote, user_pubkey):
        """Returns a base64 unsigned VersionedTransaction."""
        r = requests.post(f"{self.base}/swap", headers=self.headers, timeout=20, json={
            "quoteResponse": quote,
            "userPublicKey": str(user_pubkey),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": C.MAX_PRIORITY_FEE_LAMPORTS,
                    "priorityLevel": "high",
                }
            },
        })
        r.raise_for_status()
        data = r.json()
        if "swapTransaction" not in data:
            raise RuntimeError(f"Bad Jupiter swap response: {data}")
        return data["swapTransaction"]
