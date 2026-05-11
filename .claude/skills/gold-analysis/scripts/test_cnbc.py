import urllib.request
import json
import os

os.environ['http_proxy']='http://127.0.0.1:7890'
os.environ['https_proxy']='http://127.0.0.1:7890'

url = 'https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols=XAU='
headersStr = {
    'User-Agent': 'Mozilla/5.0'
}
try:
    req = urllib.request.Request(url, headers=headersStr)
    res = urllib.request.urlopen(req, timeout=5).read().decode()
    data = json.loads(res)
    print("SUCCESS CNBC API:", data)
except Exception as e:
    print("ERROR CNBC:", e)
