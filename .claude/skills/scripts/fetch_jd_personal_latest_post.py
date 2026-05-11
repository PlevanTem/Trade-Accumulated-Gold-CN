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

# 个人主页：feed 列表中第一条 post 的旧标题选择器（页面改版后可能失效）
FEED_TITLE_SELECTOR = "div.jue-lego-feed-container span.content-title-text"

# 个人主页：更稳的候选节点选择器。优先依赖埋点属性，而不是脆弱的 class 名。
FEED_FALLBACK_SELECTORS = [
    '[data-qidian-ext]',
    '[data-id]',
]

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


def _normalize_text(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _score_post_candidate(
    text: str,
    *,
    qidian_ext: Optional[Dict[str, Any]],
    qidian_raw: Optional[str],
) -> int:
    score = 0
    lower_text = text.lower()
    raw = qidian_raw or ""
    content_type = str((qidian_ext or {}).get("contentType", ""))

    if raw:
        score += 4
    if qidian_ext and qidian_ext.get("contentId"):
        score += 6
    if "动态" in content_type:
        score += 3
    if "金友圈" in text:
        score += 3
    if "观点精选" in text or "资讯总分析" in text or "资讯汇总分析" in text:
        score += 5
    if "评论" in text:
        score += 1
    if re.search(r"\d+分钟前|\d+小时前|昨天", text):
        score += 1
    if len(text) >= 20:
        score += 1

    # 过滤明显不是帖子正文的头部资料区/空白占位
    if "黄金持仓" in text or "关注并查看明细" in text:
        score -= 5
    if text in {"动态", "文章", "视频", "暂无相关内容"}:
        score -= 6
    if "个人主页" in lower_text:
        score -= 3

    return score


def _goto_with_retries(page, url: str, *, navigation_timeout_ms: int, attempts: int = 3) -> None:
    last_exc: Optional[Exception] = None
    for attempt_idx in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
            return
        except Exception as exc:
            last_exc = exc
            if attempt_idx == attempts - 1:
                raise
            page.wait_for_timeout(1500 * (attempt_idx + 1))
    if last_exc is not None:
        raise last_exc


def _find_latest_post_locator(
    page,
    *,
    legacy_selector: str,
    element_timeout_ms: int,
) -> tuple[Any, str, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    返回 (locator, title_text, data_id, qidian_ext, clstag)。

    先尝试旧选择器；失败后回退到埋点属性扫描，避免页面改版时完全失效。
    """
    try:
        legacy_locator = page.locator(legacy_selector).first
        legacy_locator.wait_for(state="visible", timeout=3_000)
        title_text = _normalize_text(legacy_locator.inner_text(timeout=3_000))
        qidian_raw = legacy_locator.get_attribute("data-qidian-ext")
        return (
            legacy_locator,
            title_text,
            legacy_locator.get_attribute("data-id"),
            _parse_qidian_ext(qidian_raw),
            legacy_locator.get_attribute("clstag"),
        )
    except Exception:
        pass

    best_candidate: Optional[tuple[Any, str, Optional[str], Optional[Dict[str, Any]], Optional[str], int]] = None

    for selector in FEED_FALLBACK_SELECTORS:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 80)
        except Exception:
            continue

        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                if not candidate.is_visible(timeout=800):
                    continue
                text = _normalize_text(candidate.inner_text(timeout=1_500))
                if not text:
                    continue
                qidian_raw = candidate.get_attribute("data-qidian-ext")
                qidian_ext = _parse_qidian_ext(qidian_raw)
                score = _score_post_candidate(text, qidian_ext=qidian_ext, qidian_raw=qidian_raw)
                if score <= 0:
                    continue
                normalized = (
                    candidate,
                    text,
                    candidate.get_attribute("data-id"),
                    qidian_ext,
                    candidate.get_attribute("clstag"),
                    score,
                )
                if best_candidate is None or score > best_candidate[5]:
                    best_candidate = normalized
            except Exception:
                continue

    if best_candidate is None:
        raise PlaywrightTimeoutError(
            "未能定位最新动态节点：旧选择器失效，且在埋点属性扫描中未找到可见帖子候选。"
        )

    locator, text, data_id, qidian_ext, clstag, _score = best_candidate
    return locator, text, data_id, qidian_ext, clstag


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
        published_at = _parse_published_time(raw)
        if published_at is None:
            return None, None
        return published_at, raw
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
            _goto_with_retries(page, url, navigation_timeout_ms=navigation_timeout_ms)

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

            # 页面已进入个人主页，但 feed 区内容是异步挂载的，先等关键区域出现。
            page.locator("text=动态").first.wait_for(state="visible", timeout=element_timeout_ms)

            # ── 步骤1：读取 feed 列表中第一条 post 的标题信息 ──────────────────
            locator, title_text, data_id, qidian_ext, clstag = _find_latest_post_locator(
                page,
                legacy_selector=title_selector,
                element_timeout_ms=element_timeout_ms,
            )

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
