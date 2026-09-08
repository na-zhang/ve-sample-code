#!/usr/bin/env python3
"""
AI 搜索 Demo：豆包搜索 + Seed Pro 大模型两阶段智能问答
阶段1：调用豆包搜索 API 获取实时网页结果
阶段2：将搜索结果作为上下文，调用豆包 Seed Pro 大模型生成总结回答
支持流式输出，体验类似 Agent Search
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests
from loguru import logger
from dotenv import load_dotenv

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.json import JSON as RichJSON
    from rich import box

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

load_dotenv()

SEARCH_API_URL = "https://open.feedcoopapi.com/search_api/web_search"
SEARCH_API_KEY_ENV = "DOUBAO_API_KEY"

ARK_BASE_URL_DEFAULT = "https://ark.cn-beijing.volces.com/api/v3"
ARK_API_KEY_ENV = "ARK_API_KEY"
ARK_MODEL_ENV = "ARK_MODEL"

REQUEST_TIMEOUT = 60
QUERY_MAX_LENGTH = 100
SYSTEM_PROMPT = """你是一个专业的AI搜索助手。用户会向你提问，同时你会收到从搜索引擎获取的相关网页资料。

请严格按照以下规则回答：
1. 基于提供的搜索资料进行回答，不要编造信息
2. 如果搜索资料不足以回答问题，请明确说明
3. 回答要条理清晰、准确简洁，先给出核心结论，再展开细节
4. 回答末尾，按序号列出参考来源，格式为：[n] 标题 - 站点名称
5. 使用中文回答，除非用户用其他语言提问
6. 保持客观中立，引用数据时注明来源序号"""


@dataclass
class SearchResult:
    success: bool
    web_results: List[Dict[str, Any]] = field(default_factory=list)
    result_count: int = 0
    search_time_cost_ms: int = 0
    request_id: str = ""
    latency_ms: float = 0.0
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class LLMStreamEvent:
    event_type: str
    delta_text: str = ""
    full_text: str = ""
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResult:
    success: bool
    answer: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class AISearchResult:
    success: bool
    answer: str = ""
    search_results: List[Dict[str, Any]] = field(default_factory=list)
    search_result_count: int = 0
    search_latency_ms: float = 0.0
    llm_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    search_request_id: str = ""
    llm_usage: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class DoubaoSearchClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    def web_search(
        self, query: str, count: int = 10, time_range: str = ""
    ) -> SearchResult:
        payload: Dict[str, Any] = {"Query": query, "SearchType": "web", "Count": count}
        if time_range:
            payload["TimeRange"] = time_range

        logger.info(f"[搜索] 发起请求 → {SEARCH_API_URL}")
        logger.debug(f"[搜索] 参数: {json.dumps(payload, ensure_ascii=False)}")

        start_time = time.perf_counter()
        try:
            resp = self.session.post(
                SEARCH_API_URL, json=payload, timeout=REQUEST_TIMEOUT
            )
            latency = (time.perf_counter() - start_time) * 1000
        except requests.exceptions.Timeout:
            latency = (time.perf_counter() - start_time) * 1000
            logger.error(f"[搜索] 请求超时 ({REQUEST_TIMEOUT}s)")
            return SearchResult(
                success=False,
                latency_ms=latency,
                error_code="SEARCH_TIMEOUT",
                error_message=f"搜索请求超时，超过 {REQUEST_TIMEOUT} 秒",
            )
        except requests.exceptions.ConnectionError as e:
            latency = (time.perf_counter() - start_time) * 1000
            logger.error(f"[搜索] 连接失败: {e}")
            return SearchResult(
                success=False,
                latency_ms=latency,
                error_code="SEARCH_CONN_ERROR",
                error_message=f"搜索连接失败: {e}",
            )
        except requests.exceptions.RequestException as e:
            latency = (time.perf_counter() - start_time) * 1000
            logger.error(f"[搜索] 请求异常: {e}")
            return SearchResult(
                success=False,
                latency_ms=latency,
                error_code="SEARCH_ERROR",
                error_message=str(e),
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            logger.error(f"[搜索] JSON 解析失败: {e}")
            return SearchResult(
                success=False,
                latency_ms=latency,
                error_code="SEARCH_JSON_ERROR",
                error_message=f"搜索响应 JSON 解析失败: {e}",
            )

        metadata = data.get("ResponseMetadata", {}) or {}
        request_id = metadata.get("RequestId", "")
        error_info = metadata.get("Error")

        if error_info:
            err_code = str(error_info.get("Code", error_info.get("CodeN", "UNKNOWN")))
            err_msg = error_info.get("Message", "未知错误")
            logger.error(f"[搜索] API 错误 [{err_code}]: {err_msg}")
            return SearchResult(
                success=False,
                request_id=request_id,
                latency_ms=latency,
                error_code=err_code,
                error_message=err_msg,
            )

        if resp.status_code >= 400:
            return SearchResult(
                success=False,
                request_id=request_id,
                latency_ms=latency,
                error_code=f"SEARCH_HTTP_{resp.status_code}",
                error_message=f"HTTP {resp.status_code} 错误",
            )

        result = data.get("Result") or {}
        web_results = result.get("WebResults", []) or []
        result_count = result.get("ResultCount", 0)
        server_cost = result.get("TimeCost", 0)

        logger.success(
            f"[搜索] 成功 ✓ 找到 {result_count} 条结果，"
            f"耗时: {latency:.0f}ms (服务端 {server_cost}ms)"
        )
        return SearchResult(
            success=True,
            web_results=web_results,
            result_count=result_count,
            search_time_cost_ms=server_cost,
            request_id=request_id,
            latency_ms=latency,
        )


class DoubaoLLMClient:
    def __init__(self, api_key: str, model: str, base_url: str = ARK_BASE_URL_DEFAULT):
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
        )

    def _build_messages(
        self, query: str, search_results: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        context_parts = []
        for i, item in enumerate(search_results, 1):
            title = item.get("Title", "(无标题)")
            site = item.get("SiteName", "")
            snippet = item.get("Snippet", item.get("Summary", ""))
            url = item.get("Url", "")
            context_parts.append(
                f"[来源{i}]\n标题：{title}\n站点：{site}\n链接：{url}\n内容摘要：{snippet}"
            )

        context_text = "\n\n".join(context_parts)

        user_message = f"""用户问题：{query}

以下是搜索引擎返回的相关资料：

{context_text}

请根据以上资料回答用户问题。"""

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

    def stream_chat(
        self,
        query: str,
        search_results: List[Dict[str, Any]],
        temperature: float = 0.7,
    ) -> Iterator[LLMStreamEvent]:
        messages = self._build_messages(query, search_results)
        url = f"{self.base_url}/chat/completions"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
        }

        logger.info(f"[LLM] 发起流式请求 → {url} (model={self.model})")

        start_time = time.perf_counter()
        try:
            resp = self.session.post(
                url, json=payload, stream=True, timeout=REQUEST_TIMEOUT
            )
        except requests.exceptions.Timeout:
            logger.error(f"[LLM] 请求超时 ({REQUEST_TIMEOUT}s)")
            yield LLMStreamEvent(
                event_type="error",
                finish_reason="error",
            )
            return
        except requests.exceptions.ConnectionError as e:
            logger.error(f"[LLM] 连接失败: {e}")
            yield LLMStreamEvent(
                event_type="error",
                finish_reason="error",
            )
            return
        except requests.exceptions.RequestException as e:
            logger.error(f"[LLM] 请求异常: {e}")
            yield LLMStreamEvent(
                event_type="error",
                finish_reason="error",
            )
            return

        if resp.status_code >= 400:
            logger.error(f"[LLM] HTTP 错误: {resp.status_code}")
            try:
                err_data = resp.json()
                err = err_data.get("error", {})
                err_msg = err.get("message", f"HTTP {resp.status_code}")
                err_code = str(err.get("code", f"HTTP_{resp.status_code}"))
            except Exception:
                err_code = f"HTTP_{resp.status_code}"
                err_msg = resp.text[:300]
            logger.error(f"[LLM] 错误 [{err_code}]: {err_msg}")
            yield LLMStreamEvent(
                event_type="error",
                finish_reason="error",
            )
            return

        logger.info("[LLM] 连接建立，开始接收流式响应...")
        first_token = True
        full_text = ""
        buffer = ""
        response_encoding = resp.encoding or "utf-8"
        if response_encoding.lower() in ("iso-8859-1", "latin-1"):
            response_encoding = "utf-8"

        for chunk_bytes in resp.iter_content(chunk_size=None, decode_unicode=False):
            if not chunk_bytes:
                continue

            try:
                chunk = chunk_bytes.decode(response_encoding, errors="replace")
            except Exception:
                chunk = chunk_bytes.decode("utf-8", errors="replace")

            buffer += chunk

            while "\n" in buffer:
                line_bytes, buffer = buffer.split("\n", 1)
                line = line_bytes.strip()

                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line in ("[DONE]", ""):
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                choices = data.get("choices")
                if isinstance(choices, list) and len(choices) > 0:
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    content = ""
                    if isinstance(delta, dict):
                        content = delta.get("content", "") or ""

                    if content:
                        if first_token:
                            first_token = False
                            ttf = (time.perf_counter() - start_time) * 1000
                            logger.info(f"[LLM] 首字到达 | TTFT: {ttf:.0f}ms")
                        full_text += content
                        yield LLMStreamEvent(
                            event_type="delta",
                            delta_text=content,
                            full_text=full_text,
                        )

                    finish_reason = choice.get("finish_reason", "")
                    if finish_reason:
                        usage = data.get("usage") or {}
                        total = (time.perf_counter() - start_time) * 1000
                        logger.success(
                            f"[LLM] 生成完成 | 原因: {finish_reason} | 耗时: {total:.0f}ms"
                        )
                        yield LLMStreamEvent(
                            event_type="done",
                            full_text=full_text,
                            finish_reason=finish_reason,
                            usage=usage,
                        )
                        return

                usage = data.get("usage")
                if usage and not isinstance(choices, list):
                    total = (time.perf_counter() - start_time) * 1000
                    yield LLMStreamEvent(
                        event_type="done",
                        full_text=full_text,
                        finish_reason="stop",
                        usage=usage,
                    )
                    return

        if full_text:
            total = (time.perf_counter() - start_time) * 1000
            yield LLMStreamEvent(
                event_type="done",
                full_text=full_text,
                finish_reason="stop",
            )

    def chat(
        self,
        query: str,
        search_results: List[Dict[str, Any]],
        temperature: float = 0.7,
    ) -> LLMResult:
        messages = self._build_messages(query, search_results)
        url = f"{self.base_url}/chat/completions"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
        }

        logger.info(f"[LLM] 发起请求 → {url} (model={self.model})")
        start_time = time.perf_counter()

        try:
            resp = self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            latency = (time.perf_counter() - start_time) * 1000
        except requests.exceptions.Timeout:
            latency = (time.perf_counter() - start_time) * 1000
            return LLMResult(
                success=False,
                latency_ms=latency,
                error_code="LLM_TIMEOUT",
                error_message=f"LLM 请求超时 ({REQUEST_TIMEOUT}s)",
            )
        except requests.exceptions.RequestException as e:
            latency = (time.perf_counter() - start_time) * 1000
            return LLMResult(
                success=False,
                latency_ms=latency,
                error_code="LLM_ERROR",
                error_message=str(e),
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            return LLMResult(
                success=False,
                latency_ms=latency,
                error_code="LLM_JSON_ERROR",
                error_message=f"JSON 解析失败: {e}",
            )

        if "error" in data:
            err = data["error"]
            err_code = str(err.get("code", "UNKNOWN"))
            err_msg = err.get("message", "未知错误")
            logger.error(f"[LLM] API 错误 [{err_code}]: {err_msg}")
            return LLMResult(
                success=False,
                latency_ms=latency,
                error_code=err_code,
                error_message=err_msg,
            )

        choices = data.get("choices", [])
        answer = ""
        if choices:
            message = choices[0].get("message", {})
            answer = message.get("content", "")

        usage = data.get("usage", {})
        logger.success(f"[LLM] 生成完成 ✓ 耗时: {latency:.0f}ms")

        return LLMResult(
            success=True,
            answer=answer,
            usage=usage,
            latency_ms=latency,
        )


class ResultRenderer:
    def __init__(self):
        self.console = Console() if _RICH_AVAILABLE else None

    def render_header(self, query: str, model: str, search_count: int) -> None:
        if _RICH_AVAILABLE:
            body = (
                f"[bold]问题:[/bold] {query}\n"
                f"[bold]模型:[/bold] {model}\n"
                f"[bold]搜索条数:[/bold] {search_count}"
            )
            self.console.print(
                Panel(body, title="🧠 AI 智能搜索", border_style="magenta", padding=(1, 2))
            )
        else:
            print("=" * 60)
            print(f"AI 智能搜索")
            print(f"  问题: {query}")
            print(f"  模型: {model}")
            print(f"  搜索条数: {search_count}")
            print("=" * 60)

    def render_search_status(self, result: SearchResult) -> None:
        if _RICH_AVAILABLE:
            if result.success:
                self.console.print(
                    f"[dim]🔍 搜索完成，找到 {result.result_count} 条结果 "
                    f"({result.latency_ms:.0f}ms)[/dim]"
                )
            else:
                self.console.print(f"[red]✗ 搜索失败: {result.error_message}[/red]")
        else:
            if result.success:
                print(f"\n搜索完成，找到 {result.result_count} 条结果 ({result.latency_ms:.0f}ms)")
            else:
                print(f"\n搜索失败: {result.error_message}")

    def render_streaming(
        self, event_iterator: Iterator[LLMStreamEvent]
    ) -> Tuple[str, Dict[str, Any], bool, float]:
        if _RICH_AVAILABLE:
            return self._render_streaming_rich(event_iterator)
        return self._render_streaming_plain(event_iterator)

    def _render_streaming_rich(
        self, event_iterator: Iterator[LLMStreamEvent]
    ) -> Tuple[str, Dict[str, Any], bool, float]:
        full_text = ""
        usage: Dict[str, Any] = {}
        success = True
        start_time = time.perf_counter()
        first_text = True

        with self.console.status("[cyan]正在分析搜索结果...", spinner="dots") as status:
            for event in event_iterator:
                if event.event_type == "delta":
                    if first_text:
                        first_text = False
                        status.stop()
                        self.console.print()
                        self.console.print("[bold green]💡 AI 回答:[/bold green]")
                        self.console.print()
                    full_text = event.full_text
                    self.console.print(event.delta_text, end="", markup=False, soft_wrap=True)
                    sys.stdout.flush()
                elif event.event_type == "done":
                    full_text = event.full_text
                    usage = event.usage
                    break
                elif event.event_type == "error":
                    success = False
                    break

        latency = (time.perf_counter() - start_time) * 1000
        self.console.print("\n")
        return full_text, usage, success, latency

    def _render_streaming_plain(
        self, event_iterator: Iterator[LLMStreamEvent]
    ) -> Tuple[str, Dict[str, Any], bool, float]:
        full_text = ""
        usage: Dict[str, Any] = {}
        success = True
        start_time = time.perf_counter()
        first_text = True

        print("\n正在分析搜索结果...")

        for event in event_iterator:
            if event.event_type == "delta":
                if first_text:
                    first_text = False
                    print("\n--- AI 回答 ---")
                full_text = event.full_text
                print(event.delta_text, end="", flush=True)
            elif event.event_type == "done":
                full_text = event.full_text
                usage = event.usage
                print()
                break
            elif event.event_type == "error":
                success = False
                print("\n生成失败")
                break

        latency = (time.perf_counter() - start_time) * 1000
        return full_text, usage, success, latency

    def render_summary(self, result: AISearchResult) -> None:
        if _RICH_AVAILABLE:
            self._render_summary_rich(result)
        else:
            self._render_summary_plain(result)

    def _render_summary_rich(self, result: AISearchResult) -> None:
        style = "green" if result.success else "red"
        title = f"[{style}]{'✓ 完成' if result.success else '✗ 失败'}[/{style}]"

        lines = [
            f"[bold]总耗时:[/bold] [yellow]{result.total_latency_ms:.0f} ms[/yellow]",
            f"  ├─ 搜索阶段: {result.search_latency_ms:.0f} ms",
            f"  └─ 总结阶段: {result.llm_latency_ms:.0f} ms",
        ]
        if result.search_request_id:
            lines.append(f"[bold]RequestId:[/bold] {result.search_request_id}")
        if result.llm_usage:
            pt = result.llm_usage.get("prompt_tokens", "?")
            ct = result.llm_usage.get("completion_tokens", "?")
            tt = result.llm_usage.get("total_tokens", "?")
            lines.append(f"\n[bold]📊 Token 用量:[/bold]")
            lines.append(f"  输入: {pt} | 输出: {ct} | 总计: {tt}")
        if not result.success:
            lines.append(f"\n[red][bold]错误:[/bold] [{result.error_code}] {result.error_message}[/red]")

        self.console.print(Panel("\n".join(lines), title=title, border_style=style, padding=(1, 2)))

    def _render_summary_plain(self, result: AISearchResult) -> None:
        print(f"\n{'='*60}")
        print(f"[{'✓ 完成' if result.success else '✗ 失败'}]")
        print(f"  总耗时: {result.total_latency_ms:.0f} ms")
        print(f"    搜索阶段: {result.search_latency_ms:.0f} ms")
        print(f"    总结阶段: {result.llm_latency_ms:.0f} ms")
        if result.search_request_id:
            print(f"  RequestId: {result.search_request_id}")
        if result.llm_usage:
            print(f"  Token: 输入 {result.llm_usage.get('prompt_tokens', '?')}, "
                  f"输出 {result.llm_usage.get('completion_tokens', '?')}")
        if not result.success:
            print(f"  错误: [{result.error_code}] {result.error_message}")
        print("=" * 60)

    def render_references(self, web_results: List[Dict[str, Any]]) -> None:
        if not web_results:
            return
        if _RICH_AVAILABLE:
            self._render_references_rich(web_results)
        else:
            self._render_references_plain(web_results)

    def _render_references_rich(self, web_results: List[Dict[str, Any]]) -> None:
        table = Table(
            title="📚 参考资料",
            box=box.ROUNDED,
            border_style="blue",
            show_lines=True,
        )
        table.add_column("#", style="cyan", width=4, justify="center")
        table.add_column("标题", style="bold", width=35)
        table.add_column("站点", style="green", width=18)
        table.add_column("摘要", style="white", width=50)

        for i, item in enumerate(web_results, 1):
            title = item.get("Title", "(无标题)")
            site = item.get("SiteName", "")
            snippet = item.get("Snippet", item.get("Summary", ""))
            if len(title) > 35:
                title = title[:32] + "..."
            if len(snippet) > 150:
                snippet = snippet[:147] + "..."
            table.add_row(str(i), title, site, snippet)

        self.console.print(table)

    def _render_references_plain(self, web_results: List[Dict[str, Any]]) -> None:
        print(f"\n参考资料 ({len(web_results)} 条):")
        print("-" * 60)
        for i, item in enumerate(web_results, 1):
            title = item.get("Title", "(无标题)")
            site = item.get("SiteName", "")
            url = item.get("Url", "")
            snippet = item.get("Snippet", item.get("Summary", ""))
            print(f"\n[{i}] {title}")
            print(f"    站点: {site}")
            if url:
                print(f"    链接: {url}")
            print(f"    摘要: {snippet[:200]}")


def get_env(key: str, default: str = "") -> str:
    val = os.environ.get(key, default)
    return val.strip() if val else default


def get_required_env(key: str, description: str) -> str:
    val = get_env(key)
    if not val:
        logger.error(
            f"未找到环境变量 {key}（{description}）。\n"
            f"请在 .env 文件或环境变量中配置：{key}=your_value"
        )
        sys.exit(1)
    return val


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
        description="AI 智能搜索 Demo：豆包搜索 + Seed Pro 大模型两阶段问答",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基础 AI 搜索（流式输出）
  python ai_search_demo.py --query "今天北京天气怎么样"

  # 指定搜索结果条数
  python ai_search_demo.py --query "最新AI技术进展" --search-count 15

  # 非流式输出
  python ai_search_demo.py --query "Python入门教程" --no-stream

  # 指定时间范围搜索
  python ai_search_demo.py --query "最近一周科技新闻" --time-range OneWeek

  # 不显示参考资料
  python ai_search_demo.py --query "test" --no-references

环境变量 (.env):
  DOUBAO_API_KEY   豆包搜索 API Key（必需）
  ARK_API_KEY      火山引擎方舟 API Key（必需）
  ARK_MODEL        模型 endpoint ID（必需）
  ARK_BASE_URL     API 地址（可选，默认 ark.cn-beijing.volces.com/api/v3）
        """,
    )
    parser.add_argument("--query", "-q", type=str, required=True, help="搜索问题/关键词")
    parser.add_argument(
        "--search-count", "-n", type=int, default=10, help="搜索结果条数 (默认: 10)"
    )
    parser.add_argument(
        "--time-range",
        type=str,
        default="",
        choices=["", "OneDay", "OneWeek", "OneMonth", "OneYear"],
        help="搜索时间范围",
    )
    parser.add_argument(
        "--temperature", "-t", type=float, default=0.7, help="LLM 温度参数 (默认: 0.7)"
    )
    parser.add_argument("--no-stream", action="store_true", help="非流式输出")
    parser.add_argument("--no-references", action="store_true", help="不显示参考资料")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    if not args.query.strip():
        logger.error("查询内容不能为空")
        sys.exit(1)

    query = args.query.strip()

    search_api_key = get_required_env(SEARCH_API_KEY_ENV, "豆包搜索 API Key")
    ark_api_key = get_required_env(ARK_API_KEY_ENV, "火山引擎方舟 API Key")
    ark_model = get_required_env(ARK_MODEL_ENV, "模型 Endpoint ID")
    ark_base_url = get_env("ARK_BASE_URL", ARK_BASE_URL_DEFAULT)

    renderer = ResultRenderer()
    renderer.render_header(query, ark_model, args.search_count)

    total_start = time.perf_counter()

    search_client = DoubaoSearchClient(search_api_key)
    search_result = search_client.web_search(
        query, count=args.search_count, time_range=args.time_range
    )
    renderer.render_search_status(search_result)

    if not search_result.success:
        total_latency = (time.perf_counter() - total_start) * 1000
        ai_result = AISearchResult(
            success=False,
            search_latency_ms=search_result.latency_ms,
            total_latency_ms=total_latency,
            error_code=search_result.error_code,
            error_message=search_result.error_message,
        )
        renderer.render_summary(ai_result)
        sys.exit(1)

    if not search_result.web_results:
        total_latency = (time.perf_counter() - total_start) * 1000
        logger.warning("搜索无结果，跳过 LLM 总结")
        ai_result = AISearchResult(
            success=True,
            answer="（未搜索到相关结果，无法生成回答）",
            search_results=[],
            search_result_count=0,
            search_latency_ms=search_result.latency_ms,
            llm_latency_ms=0,
            total_latency_ms=total_latency,
            search_request_id=search_result.request_id,
        )
        renderer.render_summary(ai_result)
        sys.exit(0)

    llm_client = DoubaoLLMClient(ark_api_key, ark_model, ark_base_url)

    llm_answer = ""
    llm_usage: Dict[str, Any] = {}
    llm_success = True
    llm_latency = 0.0

    if args.no_stream:
        llm_result = llm_client.chat(
            query, search_result.web_results, temperature=args.temperature
        )
        llm_latency = llm_result.latency_ms
        llm_usage = llm_result.usage
        llm_success = llm_result.success
        if llm_result.success:
            llm_answer = llm_result.answer
            if _RICH_AVAILABLE:
                renderer.console.print()
                renderer.console.print("[bold green]💡 AI 回答:[/bold green]")
                renderer.console.print()
                renderer.console.print(llm_answer)
                renderer.console.print()
            else:
                print(f"\n--- AI 回答 ---\n{llm_answer}\n")
        else:
            logger.error(f"LLM 错误: {llm_result.error_message}")
    else:
        events = llm_client.stream_chat(
            query, search_result.web_results, temperature=args.temperature
        )
        llm_answer, llm_usage, llm_success, llm_latency = renderer.render_streaming(events)

    total_latency = (time.perf_counter() - total_start) * 1000

    ai_result = AISearchResult(
        success=llm_success,
        answer=llm_answer,
        search_results=search_result.web_results,
        search_result_count=search_result.result_count,
        search_latency_ms=search_result.latency_ms,
        llm_latency_ms=llm_latency,
        total_latency_ms=total_latency,
        search_request_id=search_result.request_id,
        llm_usage=llm_usage,
        error_code="LLM_ERROR" if not llm_success else None,
        error_message="LLM 生成失败" if not llm_success else None,
    )

    renderer.render_summary(ai_result)

    if llm_success and not args.no_references:
        renderer.render_references(search_result.web_results)

    sys.exit(0 if ai_result.success else 1)


if __name__ == "__main__":
    main()
