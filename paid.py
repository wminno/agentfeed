"""Paid-tier keys for AgentFeed: USDC-on-Base verification + one-time codes."""
import re, time, threading, secrets, json as _json, urllib.request

WALLET = "0x17596BccC6B0dCa824E415583d3BB42d248084d8"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02929"
TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952b7e958e9f6f142a3a2f71fbcfc7ba"
CODES = ["AF7K2QMX", "T4RN8WVC", "H9PD5LKA"]
KEYS_PATH = "./paid_keys.json"
STATE = {"codes_used": [], "keys": {}, "txs": {}}
try:
    STATE = _json.load(open(KEYS_PATH))
except Exception:
    pass
KLOCK = threading.Lock()

def save_state():
    with open(KEYS_PATH, "w") as f:
        _json.dump(STATE, f)

def key_ok(key):
    if not key:
        return False
    k = STATE["keys"].get(key)
    return bool(k) and k.get("count", 0) < k.get("limit", 100000)

def bump_key(key):
    if not key_ok(key):
        return
    with KLOCK:
        STATE["keys"][key]["count"] += 1
        save_state()

def new_key(source):
    key = "afk_" + secrets.token_hex(16)
    STATE["keys"][key] = {"source": source, "created": time.time(), "count": 0, "limit": 100000}
    return key

RPCS = ["https://mainnet.base.org", "https://base-rpc.publicnode.com"]

def rpc(method, params):
    payload = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = "all rpc endpoints failed"
    for u in RPCS:
        try:
            req = urllib.request.Request(u, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                j = _json.loads(r.read())
                if "error" in j:
                    last = str(j["error"])
                else:
                    return j["result"]
        except Exception as e:
            last = str(e)
    raise RuntimeError(last)

def usdc_verified(tx):
    th = (tx or "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{64}", th):
        return False, "tx must be a 32-byte transaction hash (0x...)"
    try:
        r = rpc("eth_getTransactionReceipt", [th])
    except Exception as e:
        return False, "rpc error: " + str(e)[:120]
    if not r:
        return False, "transaction not found on Base (check hash / wait for confirmation)"
    if r.get("status") != "0x1":
        return False, "transaction is not successful"
    for lg in r.get("logs", []):
        t = lg.get("topics") or []
        if len(t) >= 3 and (lg.get("address") or "").lower() == USDC and t[0].lower() == TOPIC0:
            to = t[2][-40:]
            try:
                amt = int((lg.get("data") or "0x0"), 16)
            except Exception:
                continue
            if to == WALLET[2:].lower() and amt >= 5_000_000:
                return True, "ok"
    return False, "no USDC transfer of >= 5.00 USDC to %s found in this tx" % WALLET

def redeem_code(code):
    c = (code or "").strip().upper()
    if not c:
        return None
    with KLOCK:
        if c in CODES and c not in STATE["codes_used"]:
            STATE["codes_used"].append(c)
            k = new_key("code:" + c)
            save_state()
            return k
    return None
