import yfinance as yf
import os

os.environ['http_proxy']='http://127.0.0.1:7890'
os.environ['https_proxy']='http://127.0.0.1:7890'

gold = yf.Ticker("XAUUSD=X")
current_price = gold.fast_info['lastPrice']
print("SUCCESS YFINANCE SPOT GOLD: ", current_price)
