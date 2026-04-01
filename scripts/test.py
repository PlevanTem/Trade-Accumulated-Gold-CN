import urllib.request; import json
url = 'https://query1.finance.yahoo.com/v8/finance/chart/GC=F'
headers={'User-Agent': 'Mozilla/5.0'}
try:
    res = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5).read().decode()
    price = json.loads(res)['chart']['result'][0]['meta']['regularMarketPrice']
    print(price)
except Exception as e:
    print('Error:', e)
