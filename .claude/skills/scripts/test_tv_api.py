import urllib.request
import json
import os

os.environ['http_proxy']='http://127.0.0.1:7890'
os.environ['https_proxy']='http://127.0.0.1:7890'

url = 'https://scanner.tradingview.com/forex/scan'
data = b'{"symbols":{"tickers":["OANDA:XAUUSD"]},"columns":["name","close","update_mode"]}'
headersStr = {
    'User-Agent': 'Mozilla/5.0',
    'Content-Type': 'application/json'
}
try:
    req = urllib.request.Request(url, data=data, headers=headersStr)
    res = urllib.request.urlopen(req, timeout=5).read().decode()
    data = json.loads(res)
    print("SUCCESS: ", data)
except Exception as e:
    print("ERROR: ", e)
