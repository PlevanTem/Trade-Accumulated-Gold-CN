"""
抓取京东金融个人页「动态」列表中最新一条的标题节点信息及发布时间。

流程：
  1. 打开个人主页，等待 feed 列表渲染，读取第一条 post 的标题文本和 content_id。
  2. 点击该 post 进入详情页，等待顶部时间元素出现，读取精确发布时间
     （选择器：p[data-jue-name="community_top_header.jue"]，
      典型文本：'2026-04-19 13:04 山东'）。
  3. 解析出 'YYYY-MM-DD HH:MM' 部分作为 published_at 写入 JSON。

依赖:
  pip install playwright
  playwright install chromium

用法:
  python fetch_jd_personal_latest_post.py
  python fetch_jd_personal_latest_post.py --headed --wait-ms 8000
  python fetch_jd_personal_latest_post.py --storage-state jd-auth.json

登录态（页面要求登录时）: 先在本机用 Playwright 存一份 cookies，例如
  playwright codegen "https://roma.jd.com/content/personal?..." --save-storage=jd-auth.json
  在弹出浏览器中完成登录后关闭窗口；再执行
  python fetch_jd_personal_latest_post.py --storage-state jd-auth.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    PlaywrightError = Exception
    PlaywrightTimeoutError = Exception
    sync_playwright = None


DEFAULT_URL = (
    "https://roma.jd.com/content/personal?"
    "contentId=jimu_user_info-12066789&romaFileName=pageCommunityPersonal"
)

# 个人主页：feed 列表中第一条 post 的标题 span
FEED_TITLE_SELECTOR = "div.jue-lego-feed-container span.content-title-text"

# 详情页：顶部精确时间元素（data-jue-name 唯一标识）
# 典型文本："2026-04-19 13:04 山东"
DETAIL_TIME_SELECTOR = 'p[data-jue-name="community_top_header.jue"]'

# 匹配 "YYYY-MM-DD HH:MM"（允许后缀如城市名）
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}")

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LatestPostInfo:
    status: str
    url: str
    title_text: Optional[str] = None
    data_id: Optional[str] = None
    content_id: Optional[str] = None
    content_type: Optional[str] = None
    qidian_ext: Optional[Dict[str, Any]] = None
    clstag: Optional[str] = None
    published_at: Optional[str] = None   # post 详情页读取的精确发布时间，如 "2026-04-19 13:04"
    published_at_raw: Optional[str] = None  # 原始文本，如 "2026-04-19 13:04 山东"
    error: Optional[str] = None
    fetched_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None or k in ("status", "url", "fetched_at")}


def _parse_qidian_ext(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def launch_browser(playwright_obj, *, headless: bool):
    browser_path = os.getenv("PLAYWRIGHT_BROWSER_PATH")
    if browser_path:
        return playwright_obj.chromium.launch(
            headless=headless,
            executable_path=browser_path,
        )
    return playwright_obj.chromium.launch(headless=headless)


def looks_like_login_wall(page_title: str, page_url: str) -> bool:
    t = (page_title or "").lower()
    u = (page_url or "").lower()
    if "登录" in page_title or "login" in t:
        return True
    if "passport" in u or "login" in u:
        return True
    return False


def _parse_published_time(raw: str) -> Optional[str]:
    """从 '2026-04-19 13:04 山东' 中提取 '2026-04-19 13:04'，失败返回 None。"""
    m = _DATETIME_RE.search(raw)
    return m.group(0) if m else None


def _fetch_detail_published_time(
    page,
    *,
    detail_time_selector: str = DETAIL_TIME_SELECTOR,
    element_timeout_ms: int = 15_000,
    post_wait_ms: int = 2_000,
) -> tuple[Optional[str], Optional[str]]:
    """
    在详情页中读取发布时间。
    返回 (published_at, published_at_raw)，任意一步失败则返回 (None, None)。
    """
    try:
        if post_wait_ms > 0:
            page.wait_for_timeout(post_wait_ms)
        time_locator = page.locator(detail_time_selector).first
        time_locator.wait_for(state="visible", timeout=element_timeout_ms)
        raw = time_locator.inner_text(timeout=element_timeout_ms).strip()
        return _parse_published_time(raw), raw
    except Exception:
        # 详情页时间抓取失败不阻断主流程，静默降级
        return None, None


def fetch_latest_post(
    url: str,
    *,
    title_selector: str = FEED_TITLE_SELECTOR,
    detail_time_selector: str = DETAIL_TIME_SELECTOR,
    headless: bool = True,
    storage_state_path: Optional[str] = None,
    navigation_timeout_ms: int = 60_000,
    element_timeout_ms: int = 30_000,
    extra_wait_ms: int = 2_000,
    detail_wait_ms: int = 2_000,
) -> LatestPostInfo:
    fetched_at = utc_now_iso()
    if sync_playwright is None:
        return LatestPostInfo(
            status="error",
            url=url,
            error="未安装 playwright：请先 pip install playwright && playwright install chromium",
            fetched_at=fetched_at,
        )

    try:
        with sync_playwright() as pw:
            browser = launch_browser(pw, headless=headless)
            context_kwargs: Dict[str, Any] = {
                "user_agent": DESKTOP_USER_AGENT,
                "viewport": {"width": 1280, "height": 900},
                "locale": "zh-CN",
            }
            if storage_state_path:
                if not os.path.isfile(storage_state_path):
                    browser.close()
                    return LatestPostInfo(
                        status="error",
                        url=url,
                        error=f"--storage-state 文件不存在: {storage_state_path}",
                        fetched_at=fetched_at,
                    )
                context_kwargs["storage_state"] = storage_state_path
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)

            if looks_like_login_wall(page.title(), page.url):
                browser.close()
                return LatestPostInfo(
                    status="error",
                    url=url,
                    error="页面疑似跳转登录或未授权，请在 --headed 模式下手动登录后重试，或导出已登录 storage_state。",
                    fetched_at=fetched_at,
                )

            if extra_wait_ms > 0:
                page.wait_for_timeout(extra_wait_ms)

            # ── 步骤1：读取 feed 列表中第一条 post 的标题信息 ──────────────────
            locator = page.locator(title_selector).first
            locator.wait_for(state="visible", timeout=element_timeout_ms)

            title_text = locator.inner_text(timeout=element_timeout_ms).strip()
            data_id = locator.get_attribute("data-id")
            clstag = locator.get_attribute("clstag")
            qidian_raw = locator.get_attribute("data-qidian-ext")
            qidian_ext = _parse_qidian_ext(qidian_raw)

            content_id: Optional[str] = None
            content_type: Optional[str] = None
            if isinstance(qidian_ext, dict):
                cid = qidian_ext.get("contentId")
                if cid is not None:
                    content_id = str(cid)
                ct = qidian_ext.get("contentType")
                if ct is not None:
                    content_type = str(ct)

            # ── 步骤2：点击进入 post 详情页，读取精确发布时间 ─────────────────
            # 点击标题 span 触发 SPA 内页跳转；失败不阻断，降级保留 published_at=None
            published_at: Optional[str] = None
            published_at_raw: Optional[str] = None
            try:
                locator.click(timeout=element_timeout_ms)
                published_at, published_at_raw = _fetch_detail_published_time(
                    page,
                    detail_time_selector=detail_time_selector,
                    element_timeout_ms=element_timeout_ms,
                    post_wait_ms=detail_wait_ms,
                )
            except Exception:
                pass  # 点击失败静默降级，published_at 保持 None

            browser.close()

            return LatestPostInfo(
                status="ok",
                url=url,
                title_text=title_text or None,
                data_id=data_id,
                content_id=content_id,
                content_type=content_type,
                qidian_ext=qidian_ext,
                clstag=clstag,
                published_at=published_at,
                published_at_raw=published_at_raw,
                fetched_at=fetched_at,
            )
    except PlaywrightTimeoutError as exc:
        return LatestPostInfo(
            status="error",
            url=url,
            error=f"等待元素超时: {exc}",
            fetched_at=fetched_at,
        )
    except PlaywrightError as exc:
        return LatestPostInfo(
            status="error",
            url=url,
            error=str(exc),
            fetched_at=fetched_at,
        )
    except Exception as exc:  # pragma: no cover
        return LatestPostInfo(
            status="error",
            url=url,
            error=f"未预期异常: {exc}",
            fetched_at=fetched_at,
        )


def _configure_stdout_utf8() -> None:
    """避免 Windows 控制台默认 GBK 在打印 emoji 时崩溃。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    _configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="抓取京东金融个人页最新动态标题")
    parser.add_argument("--url", default=DEFAULT_URL, help="个人页完整 URL")
    parser.add_argument("--headed", action="store_true", help="有头模式，便于登录/排障")
    parser.add_argument("--wait-ms", type=int, default=2000, help="domcontentloaded 后额外等待毫秒")
    parser.add_argument("--detail-wait-ms", type=int, default=2000, help="点击进入详情页后额外等待毫秒")
    parser.add_argument(
        "--selector",
        default=FEED_TITLE_SELECTOR,
        help="覆盖 feed 列表标题选择器（高级排障）",
    )
    parser.add_argument(
        "--detail-time-selector",
        default=DETAIL_TIME_SELECTOR,
        help="覆盖详情页时间元素选择器（高级排障）",
    )
    parser.add_argument(
        "--storage-state",
        default=None,
        metavar="PATH",
        help="Playwright storage_state JSON（cookies 等），用于已登录抓取",
    )
    args = parser.parse_args(argv)

    result = fetch_latest_post(
        args.url,
        title_selector=args.selector,
        detail_time_selector=args.detail_time_selector,
        headless=not args.headed,
        storage_state_path=args.storage_state,
        extra_wait_ms=max(0, int(args.wait_ms)),
        detail_wait_ms=max(0, int(args.detail_wait_ms)),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
