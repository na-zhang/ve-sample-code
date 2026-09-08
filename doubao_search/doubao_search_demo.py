#!/usr/bin/env python3
"""
豆包搜索 API 测试脚本
支持 Global 全域搜索与 Custom 自定义搜索双模式切换
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import requests
from loguru import logger
from dotenv import load_dotenv

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.json import JSON as RichJSON
    from rich.text import Text
    from rich import box

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False


load_dotenv()

CUSTOM_API_URL = "https://open.feedcoopapi.com/search_api/web_search"
GLOBAL_API_URL = "https://open.feedcoopapi.com/search_api/global_search"
API_KEY_ENV_VAR = "DOUBAO_API_KEY"
REQUEST_TIMEOUT = 30
QUERY_MAX_LENGTH = 100


class SearchMode(str, Enum):
    GLOBAL = "global"
    CUSTOM = "custom"


class CustomSearchType(str, Enum):
    WEB = "web"
    WEB_SUMMARY = "web_summary"
    IMAGE = "image"


@dataclass
class SearchResponse:
    raw: Dict[str, Any]
    latency_ms: float
    request_id: str
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class SearchRequest:
    mode: SearchMode
    query: str
    search_type: CustomSearchType = CustomSearchType.WEB
    custom_config: Dict[str, Any] = field(default_factory=dict)


class DoubaoSearchClient:
    def __init__(self, api_key: str):
        if not api_key or not api_key.strip():
            raise ValueError("API Key 不能为空")
        self.api_key = api_key.strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    def search(self, request: SearchRequest) -> SearchResponse:
        if request.mode == SearchMode.GLOBAL:
            return self._search_global(request.query, request.custom_config)
        else:
            return self._search_custom(
                request.query, request.search_type.value, request.custom_config
            )

    def _search_global(self, query: str, config: Dict[str, Any]) -> SearchResponse:
        payload = {"Query": query}
        for key in ("DocCount", "MaxSnippetLength", "MaxImageCountPerDoc"):
            if key in config:
                payload[key] = config[key]
        return self._request(GLOBAL_API_URL, payload)

    def _search_custom(
        self, query: str, search_type: str, config: Dict[str, Any]
    ) -> SearchResponse:
        payload = {"Query": query, "SearchType": search_type}
        for key in (
            "Count",
            "Filter",
            "NeedSummary",
            "TimeRange",
            "QueryControl",
            "ContentFormats",
            "Industry",
        ):
            if key in config:
                payload[key] = config[key]
        return self._request(CUSTOM_API_URL, payload)

    def _request(self, url: str, payload: Dict[str, Any]) -> SearchResponse:
        logger.info(f"发起请求 → {url}")
        logger.debug(f"请求参数: {json.dumps(payload, ensure_ascii=False)}")

        start_time = time.perf_counter()
        try:
            response = self.session.post(
                url, json=payload, timeout=REQUEST_TIMEOUT
            )
            latency_ms = (time.perf_counter() - start_time) * 1000
        except requests.exceptions.Timeout:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"请求超时 ({REQUEST_TIMEOUT}s)")
            return SearchResponse(
                raw={},
                latency_ms=latency_ms,
                request_id="",
                success=False,
                error_code="TIMEOUT",
                error_message=f"请求超时，超过 {REQUEST_TIMEOUT} 秒",
            )
        except requests.exceptions.ConnectionError as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"连接失败: {e}")
            return SearchResponse(
                raw={},
                latency_ms=latency_ms,
                request_id="",
                success=False,
                error_code="CONNECTION_ERROR",
                error_message=f"无法连接到服务器: {e}",
            )
        except requests.exceptions.RequestException as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"请求异常: {e}")
            return SearchResponse(
                raw={},
                latency_ms=latency_ms,
                request_id="",
                success=False,
                error_code="REQUEST_ERROR",
                error_message=str(e),
            )

        logger.info(f"响应状态: HTTP {response.status_code}，耗时: {latency_ms:.2f}ms")

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            logger.error(f"响应 JSON 解析失败: {e}")
            return SearchResponse(
                raw={"raw_text": response.text[:500]},
                latency_ms=latency_ms,
                request_id="",
                success=False,
                error_code="JSON_PARSE_ERROR",
                error_message=f"响应不是有效的 JSON: {e}",
            )

        request_id = (
            data.get("ResponseMetadata", {}).get("RequestId", "")
            if isinstance(data, dict)
            else ""
        )
        error_info = (
            data.get("ResponseMetadata", {}).get("Error")
            if isinstance(data, dict)
            else None
        )

        if error_info:
            error_code = str(error_info.get("Code", error_info.get("CodeN", "UNKNOWN")))
            error_msg = error_info.get("Message", "未知错误")
            logger.error(f"API 错误 [{error_code}]: {error_msg}")
            return SearchResponse(
                raw=data,
                latency_ms=latency_ms,
                request_id=request_id,
                success=False,
                error_code=error_code,
                error_message=error_msg,
            )

        if response.status_code >= 400:
            logger.error(f"HTTP 错误: {response.status_code}")
            return SearchResponse(
                raw=data,
                latency_ms=latency_ms,
                request_id=request_id,
                success=False,
                error_code=f"HTTP_{response.status_code}",
                error_message=f"HTTP {response.status_code} 错误",
            )

        logger.success(f"请求成功 ✓ RequestId: {request_id}")
        return SearchResponse(
            raw=data,
            latency_ms=latency_ms,
            request_id=request_id,
            success=True,
        )


class ResultRenderer:
    def __init__(self):
        if _RICH_AVAILABLE:
            self.console = Console()
        else:
            self.console = None

    def render_request_summary(self, request: SearchRequest) -> None:
        if _RICH_AVAILABLE:
            self._render_request_summary_rich(request)
        else:
            self._render_request_summary_plain(request)

    def _render_request_summary_rich(self, request: SearchRequest) -> None:
        title = (
            "🌍 Global 全域搜索"
            if request.mode == SearchMode.GLOBAL
            else "🎯 Custom 自定义搜索"
        )
        body_lines = [
            f"[bold]搜索模式:[/bold] {request.mode.value}",
            f"[bold]搜索 Query:[/bold] {request.query}",
        ]
        if request.mode == SearchMode.CUSTOM:
            body_lines.append(f"[bold]SearchType:[/bold] {request.search_type.value}")
        if request.custom_config:
            body_lines.append(
                f"[bold]自定义配置:[/bold] "
                f"{json.dumps(request.custom_config, ensure_ascii=False)}"
            )
        panel = Panel(
            "\n".join(body_lines),
            title=title,
            border_style="cyan",
            padding=(1, 2),
        )
        self.console.print(panel)

    def _render_request_summary_plain(self, request: SearchRequest) -> None:
        print("=" * 60)
        mode_name = "Global 全域搜索" if request.mode == SearchMode.GLOBAL else "Custom 自定义搜索"
        print(f"[{mode_name}]")
        print(f"  搜索 Query: {request.query}")
        if request.mode == SearchMode.CUSTOM:
            print(f"  SearchType: {request.search_type.value}")
        if request.custom_config:
            print(f"  自定义配置: {json.dumps(request.custom_config, ensure_ascii=False)}")
        print("=" * 60)

    def render_response(self, response: SearchResponse) -> None:
        if _RICH_AVAILABLE:
            self._render_response_rich(response)
        else:
            self._render_response_plain(response)

    def _render_response_rich(self, response: SearchResponse) -> None:
        status_style = "green" if response.success else "red"
        status_text = "✓ 请求成功" if response.success else "✗ 请求失败"
        title = f"[{status_style}]{status_text}[/{status_style}]"

        body_lines = [
            f"[bold]请求耗时:[/bold] [yellow]{response.latency_ms:.2f} ms[/yellow]",
            f"[bold]RequestId:[/bold] {response.request_id or 'N/A'}",
        ]
        if not response.success:
            body_lines.append(
                f"[bold]错误码:[/bold] [red]{response.error_code}[/red]"
            )
            body_lines.append(
                f"[bold]错误信息:[/bold] [red]{response.error_message}[/red]"
            )

        panel = Panel(
            "\n".join(body_lines),
            title=title,
            border_style=status_style,
            padding=(1, 2),
        )
        self.console.print(panel)

    def _render_response_plain(self, response: SearchResponse) -> None:
        status = "✓ 成功" if response.success else "✗ 失败"
        print(f"\n[响应状态] {status}")
        print(f"  请求耗时: {response.latency_ms:.2f} ms")
        print(f"  RequestId: {response.request_id or 'N/A'}")
        if not response.success:
            print(f"  错误码: {response.error_code}")
            print(f"  错误信息: {response.error_message}")

    def render_results(self, response: SearchResponse, mode: SearchMode) -> None:
        if not response.success or not response.raw.get("Result"):
            return

        if mode == SearchMode.GLOBAL:
            self._render_global_results(response.raw["Result"])
        else:
            self._render_custom_results(response.raw["Result"])

    def _render_global_results(self, result: Dict[str, Any]) -> None:
        if _RICH_AVAILABLE:
            self._render_global_results_rich(result)
        else:
            self._render_global_results_plain(result)

    def _render_global_results_rich(self, result: Dict[str, Any]) -> None:
        total = result.get("TotalDocCount", 0)
        documents = result.get("Documents", [])
        error_code = result.get("ErrorCode", 0)
        error_msg = result.get("ErrorMsg", "")

        if error_code != 0:
            self.console.print(
                Panel(
                    f"[red]错误码: {error_code}\n错误信息: {error_msg}[/red]",
                    title="搜索结果错误",
                    border_style="red",
                )
            )
            return

        self.console.print(
            Panel(
                f"[bold]总结果数:[/bold] {total}\n"
                f"[bold]返回条数:[/bold] {len(documents)}",
                title="📊 搜索概览",
                border_style="blue",
                padding=(1, 2),
            )
        )

        if not documents:
            self.console.print("[yellow]未找到搜索结果[/yellow]")
            return

        table = Table(
            title="🔍 搜索结果",
            box=box.ROUNDED,
            border_style="blue",
            show_lines=True,
        )
        table.add_column("#", style="cyan", width=4, justify="center")
        table.add_column("标题", style="bold", width=40)
        table.add_column("站点", style="green", width=20)
        table.add_column("摘要", style="white", width=60)

        for doc in documents:
            rank = doc.get("Rank", 0)
            title = doc.get("Title", "(无标题)")
            hostname = doc.get("HostInfo", {}).get("Hostname", "")
            snippets = doc.get("Snippet", [])
            text_snippets = [
                s.get("Text", "") for s in snippets if s.get("Type") == "text"
            ]
            snippet_text = " ".join(text_snippets)[:200]

            if len(title) > 40:
                title = title[:37] + "..."
            if len(snippet_text) > 200:
                snippet_text = snippet_text[:197] + "..."

            table.add_row(str(rank + 1), title, hostname, snippet_text)

        self.console.print(table)

    def _render_global_results_plain(self, result: Dict[str, Any]) -> None:
        total = result.get("TotalDocCount", 0)
        documents = result.get("Documents", [])
        print(f"\n搜索概览: 总结果数={total}, 返回条数={len(documents)}")
        print("-" * 60)
        for i, doc in enumerate(documents, 1):
            title = doc.get("Title", "(无标题)")
            hostname = doc.get("HostInfo", {}).get("Hostname", "")
            url = doc.get("Url", "")
            print(f"\n[{i}] {title}")
            print(f"    站点: {hostname}")
            if url:
                print(f"    链接: {url}")
            snippets = doc.get("Snippet", [])
            text_snippets = [
                s.get("Text", "") for s in snippets if s.get("Type") == "text"
            ]
            if text_snippets:
                print(f"    摘要: {' '.join(text_snippets)[:300]}")

    def _render_custom_results(self, result: Dict[str, Any]) -> None:
        if _RICH_AVAILABLE:
            self._render_custom_results_rich(result)
        else:
            self._render_custom_results_plain(result)

    def _render_custom_results_rich(self, result: Dict[str, Any]) -> None:
        result_count = result.get("ResultCount", 0)
        time_cost = result.get("TimeCost", 0)
        log_id = result.get("LogId", "")
        web_results = result.get("WebResults", [])
        image_results = result.get("ImageResults", [])

        self.console.print(
            Panel(
                f"[bold]结果数:[/bold] {result_count}\n"
                f"[bold]服务端耗时:[/bold] {time_cost} ms\n"
                f"[bold]LogId:[/bold] {log_id}",
                title="📊 搜索概览",
                border_style="blue",
                padding=(1, 2),
            )
        )

        if web_results:
            self._render_web_results_table_rich(web_results)
        if image_results:
            self._render_image_results_rich(image_results)
        if not web_results and not image_results:
            self.console.print("[yellow]未找到搜索结果[/yellow]")

    def _render_web_results_table_rich(self, web_results: List[Dict[str, Any]]) -> None:
        table = Table(
            title="🌐 Web 搜索结果",
            box=box.ROUNDED,
            border_style="blue",
            show_lines=True,
        )
        table.add_column("#", style="cyan", width=4, justify="center")
        table.add_column("标题", style="bold", width=35)
        table.add_column("站点", style="green", width=18)
        table.add_column("权威度", style="magenta", width=10)
        table.add_column("摘要", style="white", width=50)

        for i, item in enumerate(web_results, 1):
            title = item.get("Title", "(无标题)")
            site = item.get("SiteName", "")
            auth = item.get("AuthInfoDes", "")
            snippet = item.get("Snippet", item.get("Summary", ""))

            if len(title) > 35:
                title = title[:32] + "..."
            if len(snippet) > 150:
                snippet = snippet[:147] + "..."

            table.add_row(str(i), title, site, auth, snippet)

        self.console.print(table)

    def _render_image_results_rich(self, image_results: List[Dict[str, Any]]) -> None:
        table = Table(
            title="🖼️  图片搜索结果",
            box=box.ROUNDED,
            border_style="purple",
            show_lines=True,
        )
        table.add_column("#", style="cyan", width=4, justify="center")
        table.add_column("标题", style="bold", width=40)
        table.add_column("站点", style="green", width=20)
        table.add_column("尺寸", style="yellow", width=15)
        table.add_column("图片 URL", style="blue", width=50)

        full_urls: List[str] = []

        for i, item in enumerate(image_results, 1):
            title = item.get("Title", "(无标题)")
            site = item.get("SiteName", "")
            image_info = item.get("Image", {})
            width = image_info.get("Width", "?")
            height = image_info.get("Height", "?")
            img_url = image_info.get("Url", "")
            full_urls.append(f"[{i}] {img_url}")

            if len(title) > 40:
                title = title[:37] + "..."
            if len(img_url) > 50:
                img_url = img_url[:47] + "..."

            table.add_row(str(i), title, site, f"{width}x{height}", img_url)

        self.console.print(table)
        self.console.print("\n[bold]图片完整 URL:[/bold]")
        for url_line in full_urls:
            self.console.print(url_line, soft_wrap=True)

    def _render_custom_results_plain(self, result: Dict[str, Any]) -> None:
        result_count = result.get("ResultCount", 0)
        time_cost = result.get("TimeCost", 0)
        web_results = result.get("WebResults", [])
        image_results = result.get("ImageResults", [])

        print(f"\n搜索概览: 结果数={result_count}, 服务端耗时={time_cost}ms")
        print("-" * 60)

        for i, item in enumerate(web_results, 1):
            title = item.get("Title", "(无标题)")
            site = item.get("SiteName", "")
            url = item.get("Url", "")
            auth = item.get("AuthInfoDes", "")
            snippet = item.get("Snippet", item.get("Summary", ""))
            print(f"\n[{i}] {title}")
            print(f"    站点: {site} | 权威度: {auth}")
            if url:
                print(f"    链接: {url}")
            print(f"    摘要: {snippet[:300]}")

        if image_results:
            print("\n图片结果:")
            for i, item in enumerate(image_results, 1):
                title = item.get("Title", "(无标题)")
                image_info = item.get("Image", {})
                print(f"  [{i}] {title} - {image_info.get('Url', '')}")

    def render_raw_json(self, response: SearchResponse) -> None:
        if _RICH_AVAILABLE:
            self.console.print("\n[bold]📄 完整 JSON 响应:[/bold]")
            self.console.print(RichJSON(response.raw, indent=2))
        else:
            print("\n完整 JSON 响应:")
            print(json.dumps(response.raw, indent=2, ensure_ascii=False))


def get_api_key() -> str:
    api_key = os.environ.get(API_KEY_ENV_VAR, "")
    if not api_key:
        logger.error(
            f"未找到 API Key。请设置环境变量 {API_KEY_ENV_VAR}，例如:\n"
            f"  export {API_KEY_ENV_VAR}=your_api_key_here\n"
            f"或在 .env 文件中配置。"
        )
        sys.exit(1)
    return api_key


def validate_query(query: str) -> None:
    if not query or not query.strip():
        logger.error("搜索 Query 不能为空")
        sys.exit(1)
    if len(query) > QUERY_MAX_LENGTH:
        logger.warning(
            f"Query 长度 ({len(query)}) 超过限制 ({QUERY_MAX_LENGTH})，将被截断"
        )


def parse_custom_config(config_str: str) -> Dict[str, Any]:
    if not config_str:
        return {}
    config_str = config_str.strip()

    if os.path.isfile(config_str):
        try:
            with open(config_str, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"读取配置文件失败: {e}")
            sys.exit(1)

    try:
        return json.loads(config_str)
    except json.JSONDecodeError as e:
        logger.error(f"custom-config JSON 解析失败: {e}")
        sys.exit(1)


def setup_logging(verbose: bool) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="豆包搜索 API 测试脚本 - 支持 Global 和 Custom 双模式",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # Global 全域搜索
  python doubao_search_demo.py --mode global --query "今天北京天气"

  # Custom 自定义搜索 (web 类型)
  python doubao_search_demo.py --mode custom --query "最新 AI 技术"

  # Custom 图片搜索
  python doubao_search_demo.py --mode custom --search-type image --query "风景照片"

  # Custom 搜索带自定义配置
  python doubao_search_demo.py --mode custom --query "Python教程" \\
      --custom-config '{"Count": 20, "NeedSummary": true, "TimeRange": "OneMonth"}'

  # 输出原始 JSON
  python doubao_search_demo.py --mode global --query "test" --raw
        """,
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["global", "custom"],
        required=True,
        help="搜索模式: global (全球全域) 或 custom (自定义)",
    )
    parser.add_argument(
        "--query",
        type=str,
        required=True,
        help="搜索关键词 (1~100 字符)",
    )
    parser.add_argument(
        "--search-type",
        type=str,
        choices=["web", "web_summary", "image"],
        default="web",
        help="Custom 模式的搜索类型 (默认: web)",
    )
    parser.add_argument(
        "--custom-config",
        type=str,
        default="",
        help="Custom/Global 模式的额外配置参数 (JSON 字符串或文件路径)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="输出原始 JSON 响应",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="显示详细日志",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(args.verbose)

    api_key = get_api_key()
    validate_query(args.query)

    mode = SearchMode(args.mode)
    search_type = CustomSearchType(args.search_type)
    custom_config = parse_custom_config(args.custom_config)

    request = SearchRequest(
        mode=mode,
        query=args.query,
        search_type=search_type,
        custom_config=custom_config,
    )

    renderer = ResultRenderer()
    renderer.render_request_summary(request)

    try:
        client = DoubaoSearchClient(api_key)
    except ValueError as e:
        logger.error(f"初始化客户端失败: {e}")
        sys.exit(1)

    response = client.search(request)

    renderer.render_response(response)

    if response.success:
        renderer.render_results(response, mode)

    if args.raw:
        renderer.render_raw_json(response)

    sys.exit(0 if response.success else 1)


if __name__ == "__main__":
    main()
