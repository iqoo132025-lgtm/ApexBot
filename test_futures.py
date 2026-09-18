import hashlib, hmac, time, json
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import os  # المفاتيح تُقرأ من متغيرات البيئة ولا تُكتب داخل الكود

API_KEY = os.getenv("BINANCE_API_KEY", "")
SECRET  = os.getenv("BINANCE_API_SECRET", "")
BASE    = "https://fapi.binance.com"

# مزامنة التوقيت مع Binance
with urlopen(Request(BASE + "/fapi/v1/time"), timeout=5) as r:
    srv_time = json.loads(r.read())["serverTime"]
offset = srv_time - int(time.time()*1000)
ts  = int(time.time()*1000) + offset
q   = "timestamp=" + str(ts)
sig = hmac.new(SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
url = BASE + "/fapi/v2/balance?" + q + "&signature=" + sig
req = Request(url, headers={"X-MBX-APIKEY": API_KEY})
try:
    with urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
        for b in data:
            if b["asset"] == "USDT":
                print("USDT available:", b.get("availableBalance"))
                print("USDT balance:  ", b.get("balance"))
                break
        else:
            print("USDT not found — assets:", [b["asset"] for b in data[:5]])
except HTTPError as e:
    err = json.loads(e.read())
    print("HTTP Error:", e.code, err)
except Exception as e:
    print("Error:", type(e).__name__, e)
