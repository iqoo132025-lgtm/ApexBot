import hashlib, hmac, time, json
from urllib.parse import urlencode
from urllib.request import urlopen, Request
import os  # المفاتيح تُقرأ من متغيرات البيئة ولا تُكتب داخل الكود

API_KEY = os.getenv("BINANCE_API_KEY", "")
SECRET  = os.getenv("BINANCE_API_SECRET", "")
BASE    = 'https://api.binance.com'

with urlopen(Request(BASE+'/api/v3/time'),timeout=5) as r:
    off = json.loads(r.read())['serverTime'] - int(time.time()*1000)

params = {'timestamp': int(time.time()*1000)+off-100}
q = urlencode(params)
params['signature'] = hmac.new(SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
url = BASE+'/api/v3/account?'+urlencode(params)
req = Request(url, headers={'X-MBX-APIKEY': API_KEY})
with urlopen(req, timeout=10) as r:
    data = json.loads(r.read())
    print("=== رصيد Spot ===")
    found = False
    for b in data['balances']:
        if float(b['free']) > 0.001 or float(b['locked']) > 0.001:
            print(f"{b['asset']}: free={b['free']} locked={b['locked']}")
            found = True
    if not found:
        print("المحفظة فارغة — كل الرصيد في Futures")
