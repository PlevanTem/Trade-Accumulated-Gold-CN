import urllib.request
import json
import time

def fetch_realtime_gold_and_usd():
    """采用公共API获取抗幻觉的实在现资金价"""
    # 获取黄金 (XAU/USD) - 这里使用 tether-gold 作为锚定物理黄金现货的价格代表来测试
    gold_url = 'https://api.coingecko.com/api/v3/simple/price?ids=tether-gold&vs_currencies=usd'
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    try:
        req = urllib.request.Request(gold_url, headers=headers)
        res = urllib.request.urlopen(req, timeout=10).read().decode()
        data = json.loads(res)
        xau_usd = data.get('tether-gold', {}).get('usd', 0.0)
    except Exception as e:
        xau_usd = f"Error fetching Gold: {e}"
        
    return xau_usd

if __name__ == '__main__':
    print("=======================================")
    print("📈 实盘抗幻觉数据获取模块测试")
    print("=======================================")
    current_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    print(f"当前时间: {current_time}")
    
    xau_usd = fetch_realtime_gold_and_usd()
    print(f"[1] XAUUSD 现货黄金/美元: ${xau_usd}")
    
    # 按照汇率和折溢价估测当前国内积存金价（固定范例）
    if isinstance(xau_usd, (int, float)) and xau_usd > 0:
        # 使用大概参考汇率进行基准测算 (6.899 - 7.2) 
        # 这里为代码展示测算，实际中可以使用获取USDCNY的外汇API
        exchange_rate = 6.9  # 示例汇率
        gram_price_theoretical = (xau_usd * exchange_rate) / 31.1035
        
        # 折价测算 (通常积存金可能溢价或者折价)
        discount = -10.0
        calculated_zheshang = gram_price_theoretical + discount
        calculated_minsheng = gram_price_theoretical + discount + 0.5
        
        print(f"[2] 浙商积存金(推算基准价): 约为 {calculated_zheshang:.2f} 元/克")
        print(f"[3] 民生积存金(推算基准价): 约为 {calculated_minsheng:.2f} 元/克")
    print("=======================================")
