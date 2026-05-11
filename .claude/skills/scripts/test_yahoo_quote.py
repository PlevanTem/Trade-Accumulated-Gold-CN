import urllib.request
import json
import os

os.environ['http_proxy']='http://127.0.0.1:7890'
os.environ['https_proxy']='http://127.0.0.1:7890'

url = 'https://query1.finance.yahoo.com/v7/finance/quote?symbols=XAUUSD=X'
headersStr = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/100.0.0.0'
}
try:
    req = urllib.request.Request(url, headers=headersStr)
    res = urllib.request.urlopen(req, timeout=5).read().decode()
    data = json.loads(res)
    results = data['quoteResponse']['result']
    if len(results) > 0:
        print("SUCCESS XAU_TO_USD:", results[0]['regularMarketPrice'])
    else:
        print("ERROR: empty result")
except Exception as e:
    print("ERROR:", e)
