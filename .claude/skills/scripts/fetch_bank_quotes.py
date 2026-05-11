import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency at runtime
    PlaywrightError = Exception
    PlaywrightTimeoutError = Exception
    sync_playwright = None


MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

JD_LATEST_PRICE_URL = "https://api.jdjygold.com/gw/generic/hj/h5/m/latestPrice"
REQUEST_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class BankPageConfig:
    key: str
    display_name: str
    url: str
    xpath: str


BANK_PAGE_CONFIGS = [
    BankPageConfig(
        key="zheshang",
        display_name="浙商积存金",
        url="https://m.jdjygold.com/finance-gold/gold-standard/home/?productSku=1961543816",
        xpath='//*[@id="mainCSC"]/div/div[2]/div',
    ),
    BankPageConfig(
        key="minsheng",
        display_name="民生积存金",
        url="https://m.jdjygold.com/finance-gold/newgold/home/",
        xpath="/html/body/div[1]/div/div[2]/div[3]/div[1]/div[1]",
    ),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_price(text: str) -> Optional[float]:
    cleaned = normalize_text(text).replace(",", "")
    matches = re.findall(r"(?<!\d)(\d{3,5}(?:\.\d{1,2})?)(?!\d)", cleaned)
    if not matches:
        return None
    numeric_values = [float(item) for item in matches]
    plausible_values = [value for value in numeric_values if 500 <= value <= 3000]
    if plausible_values:
        return plausible_values[0]
    return numeric_values[0] if numeric_values else None


def compact_error_message(message: str) -> str:
    if not message:
        return "未知错误"
    return message.strip().splitlines()[0]


def fetch_jd_latest_price() -> Dict[str, Any]:
    response = requests.post(
        JD_LATEST_PRICE_URL,
        headers={"User-Agent": MOBILE_USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    datas = payload.get("resultData", {}).get("datas", {})
    price = datas.get("price")
    return {
        "source": "jd_latest_price_api",
        "url": JD_LATEST_PRICE_URL,
        "price": float(price) if price is not None else None,
        "raw": datas,
        "fetched_at": utc_now_iso(),
    }


def launch_browser(playwright_obj):
    browser_path = os.getenv("PLAYWRIGHT_BROWSER_PATH")
    if browser_path:
        return playwright_obj.chromium.launch(
            headless=True,
            executable_path=browser_path,
        )
    return playwright_obj.chromium.launch(headless=True)


def fetch_quote_from_page(config: BankPageConfig) -> Dict[str, Any]:
    if sync_playwright is None:
        return {
            "status": "error",
            "source": "playwright_unavailable",
            "url": config.url,
            "xpath": config.xpath,
            "error": "Python package playwright is unavailable in current environment.",
            "fetched_at": utc_now_iso(),
        }

    try:
        with sync_playwright() as playwright_obj:
            browser = launch_browser(playwright_obj)
            page = browser.new_page(
                user_agent=MOBILE_USER_AGENT,
                viewport={"width": 430, "height": 932},
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True,
            )
            page.goto(config.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            locator = page.locator(f"xpath={config.xpath}")
            locator.wait_for(state="visible", timeout=15000)
            raw_text = normalize_text(locator.inner_text(timeout=15000))
            price = extract_price(raw_text)
            browser.close()

            if price is None:
                return {
                    "status": "error",
                    "source": "playwright_xpath",
                    "url": config.url,
                    "xpath": config.xpath,
                    "raw_text": raw_text,
                    "error": "XPath命中了元素，但未能从文本中解析出价格。",
                    "fetched_at": utc_now_iso(),
                }

            return {
                "status": "ok",
                "source": "playwright_xpath",
                "url": config.url,
                "xpath": config.xpath,
                "raw_text": raw_text,
                "price": price,
                "fetched_at": utc_now_iso(),
            }
    except PlaywrightTimeoutError as exc:
        return {
            "status": "error",
            "source": "playwright_xpath",
            "url": config.url,
            "xpath": config.xpath,
            "error": f"页面或元素等待超时: {exc}",
            "fetched_at": utc_now_iso(),
        }
    except PlaywrightError as exc:
        return {
            "status": "error",
            "source": "playwright_xpath",
            "url": config.url,
            "xpath": config.xpath,
            "error": str(exc),
            "fetched_at": utc_now_iso(),
        }
    except Exception as exc:
        return {
            "status": "error",
            "source": "playwright_xpath",
            "url": config.url,
            "xpath": config.xpath,
            "error": str(exc),
            "fetched_at": utc_now_iso(),
        }


def fetch_bank_quotes() -> Dict[str, Any]:
    jd_latest_price: Optional[Dict[str, Any]] = None
    jd_latest_price_error: Optional[str] = None
    try:
        jd_latest_price = fetch_jd_latest_price()
    except Exception as exc:
        jd_latest_price_error = str(exc)

    results: Dict[str, Any] = {
        "fetched_at": utc_now_iso(),
        "jd_latest_price": jd_latest_price,
        "banks": {},
    }

    for config in BANK_PAGE_CONFIGS:
        bank_result = fetch_quote_from_page(config)
        if bank_result.get("status") != "ok" and jd_latest_price is not None:
            bank_result["fallback_price"] = jd_latest_price.get("price")
            bank_result["fallback_source"] = jd_latest_price.get("source")
            bank_result["fallback_note"] = (
                "页面XPath抓取失败，已附带京东金通用实时价作兜底参考；"
                "该价格未必等同于银行实际成交价。"
            )
        results["banks"][config.key] = bank_result

    if jd_latest_price is None:
        results["jd_latest_price_error"] = jd_latest_price_error

    return results


def print_human_summary(results: Dict[str, Any]) -> None:
    print("=" * 60)
    print("京东金积存金页面报价抓取")
    print(f"抓取时间(UTC): {results['fetched_at']}")
    print("-" * 60)

    jd_latest = results.get("jd_latest_price")
    if jd_latest and jd_latest.get("price") is not None:
        print(
            f"[兜底基准] 京东金 latestPrice: {jd_latest['price']:.2f} 元/克 "
            f"({jd_latest['url']})"
        )
    else:
        print("[兜底基准] 京东金 latestPrice: 获取失败")

    print("-" * 60)

    for config in BANK_PAGE_CONFIGS:
        bank_data = results["banks"][config.key]
        if bank_data.get("status") == "ok":
            print(
                f"[{config.display_name}] {bank_data['price']:.2f} 元/克 "
                f"| 来源: {bank_data['source']}"
            )
            print(f"  文本: {bank_data.get('raw_text', '')}")
        else:
            print(f"[{config.display_name}] 页面抓取失败")
            print(f"  原因: {compact_error_message(bank_data.get('error', '未知错误'))}")
            if bank_data.get("fallback_price") is not None:
                print(
                    f"  兜底参考: {bank_data['fallback_price']:.2f} 元/克 "
                    f"({bank_data['fallback_source']})"
                )
            if "Executable doesn't exist" in bank_data.get("error", ""):
                print(
                    "  提示: 运行 `python -m playwright install chromium` 可启用本机页面 XPath；"
                    "或在 Cursor 对话中让 Agent 用内置浏览器(MCP cursor-ide-browser)打开同一 URL 读价。"
                )
        print(f"  URL: {config.url}")
        print(f"  XPath: {config.xpath}")
        print("-" * 60)

    print("JSON:")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    print_human_summary(fetch_bank_quotes())
