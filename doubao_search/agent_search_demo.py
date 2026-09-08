#!/usr/bin/env python3
"""
联网问答 Agent API 测试脚本
基于豆包搜索 Collab 版智能体 API，支持流式输出大模型总结回答 + 搜索参考资料
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

import requests
from loguru import logger
from dotenv import load_dotenv

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.json import JSON as RichJSON
    from rich.text import Text
    from rich.live import Live
    from rich.markdown import Markdown
    from rich import box
    from rich.status import Status

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False


load_dotenv()

AGENT_API_URL = "https://open.feedcoopapi.com/agent_api/agent/collab"
API_KEY_ENV_VAR = "AGENT_API_KEY"
REQUEST_TIMEOUT = 60
QUERY_MAX_LENGTH = 100


@dataclass
class AgentStreamEvent:
    event_type: str
    raw: Dict[str, Any] = field(default_factory=dict)
    search_results: List[Dict[str, Any]] = field(default_factory=list)
    result_count: int = 0
    delta_text: str = ""
    full_text: str = ""
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    time_cost: int = 0
    log_id: str = ""
    card_results: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentResponse:
    success: bool
    answer: str = ""
    search_results: List[Dict[str, Any]] = field(default_factory=list)
    result_count: int = 0
    usage: Dict[str, Any] = field(default_factory=dict)
    time_cost_ms: float = 0.0
    total_latency_ms: float = 0.0
    request_id: str = ""
    log_id: str = ""
    card_results: List[Dict[str, Any]] = field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class AgentSearchClient:
    def __init__(self, api_key: str):
        if not api_key or not api_key.strip():
            raise ValueError("API Key 不能为空")
        self.api_key = api_key.strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
        )

    def stream_ask(
        self, query: str, config: Optional[Dict[str, Any]] = None
    ) -> Iterator[AgentStreamEvent]:
        payload = self._build_payload(query, config)
        logger.info(f"发起 Agent 请求 → {AGENT_API_URL}")
        logger.debug(f"请求参数: {json.dumps(payload, ensure_ascii=False)}")

        start_time = time.perf_counter()

        try:
            response = self.session.post(
                AGENT_API_URL,
                json=payload,
                stream=True,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.Timeout:
            latency = (time.perf_counter() - start_time) * 1000
            logger.error(f"请求超时 ({REQUEST_TIMEOUT}s)")
            yield AgentStreamEvent(
                event_type="error",
                delta_text="",
                finish_reason="error",
            )
            return
        except requests.exceptions.ConnectionError as e:
            logger.error(f"连接失败: {e}")
            yield AgentStreamEvent(
                event_type="error",
                delta_text="",
                finish_reason="error",
            )
            return
        except requests.exceptions.RequestException as e:
            logger.error(f"请求异常: {e}")
            yield AgentStreamEvent(
                event_type="error",
                delta_text="",
                finish_reason="error",
            )
            return

        if response.status_code >= 400:
            logger.error(f"HTTP 错误: {response.status_code}")
            try:
                err_data = response.json()
                err = err_data.get("ResponseMetadata", {}).get("Error", {})
                err_code = str(err.get("Code", err.get("CodeN", response.status_code)))
                err_msg = err.get("Message", f"HTTP {response.status_code}")
            except Exception:
                err_code = f"HTTP_{response.status_code}"
                err_msg = response.text[:200]

            yield AgentStreamEvent(
                event_type="error",
                delta_text="",
                finish_reason="error",
            )
            return

        logger.info(f"连接建立，开始接收流式响应...")

        full_text = ""
        search_results: List[Dict[str, Any]] = []
        result_count = 0
        usage: Dict[str, Any] = {}
        time_cost = 0
        log_id = ""
        card_results: List[Dict[str, Any]] = []
        request_id = ""

        buffer = b""
        first_frame_received = False

        try:
            for chunk in response.iter_content(chunk_size=None, decode_unicode=False):
                if not chunk:
                    continue
                buffer += chunk

                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()

                    if not line:
                        continue

                    if line.startswith("data:"):
                        line = line[5:].strip()

                    if line == "[DONE]" or line == "":
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    metadata = data.get("ResponseMetadata", {})
                    if metadata:
                        request_id = metadata.get("RequestId", request_id)
                        if metadata.get("Error"):
                            err = metadata["Error"]
                            err_code = str(err.get("Code", err.get("CodeN", "UNKNOWN")))
                            err_msg = err.get("Message", "未知错误")
                            logger.error(f"API 错误 [{err_code}]: {err_msg}")
                            yield AgentStreamEvent(
                                event_type="error",
                                raw=data,
                                delta_text="",
                                finish_reason="error",
                            )
                            return

                    result = data.get("Result")
                    if result is None:
                        continue

                    if not isinstance(result, dict):
                        continue

                    if not first_frame_received:
                        first_frame_received = True
                        search_results = result.get("WebResults", []) or []
                        result_count = result.get("ResultCount", 0)
                        time_cost = result.get("TimeCost", 0)
                        log_id = result.get("LogId", "")
                        card_results = result.get("CardResults", []) or []

                        latency = (time.perf_counter() - start_time) * 1000
                        logger.info(
                            f"首帧到达 | 搜索结果: {result_count} 条 | "
                            f"搜索耗时: {time_cost}ms | 总耗时: {latency:.0f}ms"
                        )

                        yield AgentStreamEvent(
                            event_type="search_results",
                            raw=data,
                            search_results=search_results,
                            result_count=result_count,
                            time_cost=time_cost,
                            log_id=log_id,
                            card_results=card_results,
                        )

                    choices = result.get("Choices")
                    if not isinstance(choices, list) or len(choices) == 0:
                        continue

                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue

                        delta = choice.get("Delta") or {}
                        message = choice.get("Message") or {}
                        finish_reason = choice.get("FinishReason", "") or ""

                        delta_text = ""
                        if isinstance(delta, dict):
                            delta_text = delta.get("content") or delta.get("Content") or ""

                        if delta_text:
                            full_text += delta_text
                            yield AgentStreamEvent(
                                event_type="delta",
                                raw=data,
                                delta_text=delta_text,
                                full_text=full_text,
                                finish_reason=finish_reason,
                            )
                        elif isinstance(message, dict):
                            msg_content = message.get("content") or message.get("Content") or ""
                            if msg_content and not full_text:
                                full_text = msg_content
                                yield AgentStreamEvent(
                                    event_type="full_message",
                                    raw=data,
                                    delta_text=msg_content,
                                    full_text=full_text,
                                    finish_reason=finish_reason,
                                )

                        if finish_reason and finish_reason not in ("null", "", None):
                            usage = result.get("Usage", {}) or {}
                            total_latency = (time.perf_counter() - start_time) * 1000
                            logger.success(
                                f"生成完成 | 原因: {finish_reason} | "
                                f"总耗时: {total_latency:.0f}ms"
                            )
                            yield AgentStreamEvent(
                                event_type="done",
                                raw=data,
                                full_text=full_text,
                                finish_reason=finish_reason,
                                usage=usage,
                                search_results=search_results,
                                result_count=result_count,
                                time_cost=time_cost,
                                log_id=log_id,
                                card_results=card_results,
                            )
                            return

            if full_text:
                usage = result.get("Usage", {}) if isinstance(result, dict) else {}
                yield AgentStreamEvent(
                    event_type="done",
                    full_text=full_text,
                    finish_reason="stop",
                    usage=usage,
                    search_results=search_results,
                    result_count=result_count,
                    time_cost=time_cost,
                    log_id=log_id,
                    card_results=card_results,
                )

        except Exception as e:
            logger.exception(f"流式响应解析异常: {e}")
            yield AgentStreamEvent(
                event_type="error",
                delta_text="",
                finish_reason="error",
            )

    def ask(self, query: str, config: Optional[Dict[str, Any]] = None) -> AgentResponse:
        payload = self._build_payload(query, config)
        logger.info(f"发起 Agent 请求 → {AGENT_API_URL}")
        logger.debug(f"请求参数: {json.dumps(payload, ensure_ascii=False)}")

        start_time = time.perf_counter()

        try:
            response = self.session.post(
                AGENT_API_URL,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            total_latency = (time.perf_counter() - start_time) * 1000
        except requests.exceptions.Timeout:
            total_latency = (time.perf_counter() - start_time) * 1000
            logger.error(f"请求超时 ({REQUEST_TIMEOUT}s)")
            return AgentResponse(
                success=False,
                total_latency_ms=total_latency,
                error_code="TIMEOUT",
                error_message=f"请求超时，超过 {REQUEST_TIMEOUT} 秒",
            )
        except requests.exceptions.ConnectionError as e:
            total_latency = (time.perf_counter() - start_time) * 1000
            logger.error(f"连接失败: {e}")
            return AgentResponse(
                success=False,
                total_latency_ms=total_latency,
                error_code="CONNECTION_ERROR",
                error_message=f"无法连接到服务器: {e}",
            )
        except requests.exceptions.RequestException as e:
            total_latency = (time.perf_counter() - start_time) * 1000
            logger.error(f"请求异常: {e}")
            return AgentResponse(
                success=False,
                total_latency_ms=total_latency,
                error_code="REQUEST_ERROR",
                error_message=str(e),
            )

        logger.info(f"响应状态: HTTP {response.status_code}，耗时: {total_latency:.2f}ms")

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            logger.error(f"响应 JSON 解析失败: {e}")
            return AgentResponse(
                success=False,
                total_latency_ms=total_latency,
                error_code="JSON_PARSE_ERROR",
                error_message=f"响应不是有效的 JSON: {e}",
            )

        request_id = data.get("ResponseMetadata", {}).get("RequestId", "")
        error_info = data.get("ResponseMetadata", {}).get("Error")

        if error_info:
            error_code = str(error_info.get("Code", error_info.get("CodeN", "UNKNOWN")))
            error_msg = error_info.get("Message", "未知错误")
            logger.error(f"API 错误 [{error_code}]: {error_msg}")
            return AgentResponse(
                success=False,
                total_latency_ms=total_latency,
                request_id=request_id,
                error_code=error_code,
                error_message=error_msg,
            )

        if response.status_code >= 400:
            return AgentResponse(
                success=False,
                total_latency_ms=total_latency,
                request_id=request_id,
                error_code=f"HTTP_{response.status_code}",
                error_message=f"HTTP {response.status_code} 错误",
            )

        result = data.get("Result", {})
        choices = result.get("Choices") or []
        answer = ""
        if choices and isinstance(choices, list):
            message = choices[0].get("Message") or {}
            if isinstance(message, dict):
                answer = message.get("content") or message.get("Content") or ""

        usage = result.get("Usage", {})
        time_cost = result.get("TimeCost", 0)
        log_id = result.get("LogId", "")

        logger.success(f"请求成功 ✓ RequestId: {request_id}")

        return AgentResponse(
            success=True,
            answer=answer,
            search_results=result.get("WebResults", []),
            result_count=result.get("ResultCount", 0),
            usage=usage,
            time_cost_ms=time_cost,
            total_latency_ms=total_latency,
            request_id=request_id,
            log_id=log_id,
            card_results=result.get("CardResults", []),
        )

    def _build_payload(
        self, query: str, config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        payload = {"Query": query, "NeedSummary": True}
        if config:
            for key in (
                "Count",
                "Filter",
                "TimeRange",
                "QueryControl",
                "ContentFormats",
                "Industry",
                "NeedSummary",
            ):
                if key in config:
                    payload[key] = config[key]
        return payload


class AgentResultRenderer:
    def __init__(self):
        if _RICH_AVAILABLE:
            self.console = Console()
        else:
            self.console = None

    def render_header(self, query: str, config: Dict[str, Any]) -> None:
        if _RICH_AVAILABLE:
            body_lines = [
                f"[bold]搜索 Query:[/bold] {query}",
            ]
            if config:
                body_lines.append(
                    f"[bold]自定义配置:[/bold] "
                    f"{json.dumps(config, ensure_ascii=False)}"
                )
            panel = Panel(
                "\n".join(body_lines),
                title="🤖 联网问答 Agent",
                border_style="magenta",
                padding=(1, 2),
            )
            self.console.print(panel)
        else:
            print("=" * 60)
            print("[联网问答 Agent]")
            print(f"  搜索 Query: {query}")
            if config:
                print(f"  自定义配置: {json.dumps(config, ensure_ascii=False)}")
            print("=" * 60)

    def render_streaming(
        self, event_iterator: Iterator[AgentStreamEvent]
    ) -> AgentResponse:
        if not _RICH_AVAILABLE:
            return self._render_streaming_plain(event_iterator)
        return self._render_streaming_rich(event_iterator)

    def _render_streaming_rich(
        self, event_iterator: Iterator[AgentStreamEvent]
    ) -> AgentResponse:
        self.console.print("\n[dim]🔍 正在搜索网络信息...[/dim]")

        full_text = ""
        search_results: List[Dict[str, Any]] = []
        result_count = 0
        usage: Dict[str, Any] = {}
        time_cost = 0
        log_id = ""
        card_results: List[Dict[str, Any]] = []
        success = True
        first_text = True
        error_code = None
        error_message = None
        start_time = time.perf_counter()

        with self.console.status(
            "[cyan]思考中...", spinner="dots"
        ) as status:
            for event in event_iterator:
                if event.event_type == "search_results":
                    search_results = event.search_results
                    result_count = event.result_count
                    time_cost = event.time_cost
                    log_id = event.log_id
                    card_results = event.card_results

                    status.update(
                        f"[green]✓ 找到 {result_count} 条搜索结果，正在总结..."
                    )

                elif event.event_type in ("delta", "full_message"):
                    if first_text:
                        first_text = False
                        status.stop()
                        self.console.print()
                        self.console.print(
                            "[bold green]💡 AI 回答:[/bold green]"
                        )
                        self.console.print()

                    delta = event.delta_text
                    full_text = event.full_text
                    self.console.print(delta, end="", markup=False, soft_wrap=True)
                    sys.stdout.flush()

                elif event.event_type == "done":
                    full_text = event.full_text
                    usage = event.usage
                    search_results = event.search_results
                    result_count = event.result_count
                    time_cost = event.time_cost
                    log_id = event.log_id
                    card_results = event.card_results
                    break

                elif event.event_type == "error":
                    success = False
                    error_code = "STREAM_ERROR"
                    error_message = "流式响应解析异常"
                    break

        total_latency = (time.perf_counter() - start_time) * 1000
        self.console.print("\n")

        return AgentResponse(
            success=success,
            answer=full_text,
            search_results=search_results,
            result_count=result_count,
            usage=usage,
            time_cost_ms=time_cost,
            total_latency_ms=total_latency,
            log_id=log_id,
            card_results=card_results,
            error_code=error_code,
            error_message=error_message,
        )

    def _render_streaming_plain(
        self, event_iterator: Iterator[AgentStreamEvent]
    ) -> AgentResponse:
        print("\n正在搜索网络信息...")

        full_text = ""
        search_results: List[Dict[str, Any]] = []
        result_count = 0
        usage: Dict[str, Any] = {}
        time_cost = 0
        log_id = ""
        success = True
        first_text = True
        error_code = None
        error_message = None
        start_time = time.perf_counter()

        for event in event_iterator:
            if event.event_type == "search_results":
                search_results = event.search_results
                result_count = event.result_count
                time_cost = event.time_cost
                log_id = event.log_id
                print(f"  ✓ 找到 {result_count} 条搜索结果，正在总结...")

            elif event.event_type in ("delta", "full_message"):
                if first_text:
                    first_text = False
                    print("\n--- AI 回答 ---")
                full_text = event.full_text
                print(event.delta_text, end="", flush=True)

            elif event.event_type == "done":
                full_text = event.full_text
                usage = event.usage
                search_results = event.search_results
                result_count = event.result_count
                time_cost = event.time_cost
                log_id = event.log_id
                print()
                break

            elif event.event_type == "error":
                success = False
                error_code = "STREAM_ERROR"
                error_message = "流式响应解析异常"
                print("\n请求出错")
                break

        total_latency = (time.perf_counter() - start_time) * 1000

        return AgentResponse(
            success=success,
            answer=full_text,
            search_results=search_results,
            result_count=result_count,
            usage=usage,
            time_cost_ms=time_cost,
            total_latency_ms=total_latency,
            log_id=log_id,
            error_code=error_code,
            error_message=error_message,
        )

    def render_response_summary(self, response: AgentResponse) -> None:
        if _RICH_AVAILABLE:
            self._render_response_summary_rich(response)
        else:
            self._render_response_summary_plain(response)

    def _render_response_summary_rich(self, response: AgentResponse) -> None:
        status_style = "green" if response.success else "red"
        status_text = "✓ 回答完成" if response.success else "✗ 请求失败"

        body_lines = [
            f"[bold]总耗时:[/bold] [yellow]{response.total_latency_ms:.2f} ms[/yellow]",
        ]
        if response.time_cost_ms:
            body_lines.append(
                f"[bold]搜索耗时:[/bold] {response.time_cost_ms} ms"
            )
        if response.request_id:
            body_lines.append(f"[bold]RequestId:[/bold] {response.request_id}")
        if response.log_id:
            body_lines.append(f"[bold]LogId:[/bold] {response.log_id}")

        if response.usage:
            body_lines.append("\n[bold]📊 Token 使用:[/bold]")
            prompt_tokens = response.usage.get("PromptTokens", "?")
            completion_tokens = response.usage.get("CompletionTokens", "?")
            total_tokens = response.usage.get("TotalTokens", "?")
            body_lines.append(f"  输入 Token: {prompt_tokens}")
            body_lines.append(f"  输出 Token: {completion_tokens}")
            body_lines.append(f"  总 Token:  {total_tokens}")

            search_time = response.usage.get("SearchTimeCost")
            first_token_time = response.usage.get("FirstTokenTimeCost")
            total_time = response.usage.get("TotalTimeCost")
            if search_time or first_token_time or total_time:
                body_lines.append("\n[bold]⏱️  时间消耗:[/bold]")
                if search_time:
                    body_lines.append(f"  搜索耗时: {search_time} ms")
                if first_token_time:
                    body_lines.append(f"  首帧耗时: {first_token_time} ms")
                if total_time:
                    body_lines.append(f"  总耗时:   {total_time} ms")

        if not response.success:
            body_lines.append(
                f"\n[red][bold]错误码:[/bold] {response.error_code}[/red]"
            )
            body_lines.append(
                f"[red][bold]错误信息:[/bold] {response.error_message}[/red]"
            )

        panel = Panel(
            "\n".join(body_lines),
            title=f"[{status_style}]{status_text}[/{status_style}]",
            border_style=status_style,
            padding=(1, 2),
        )
        self.console.print(panel)

    def _render_response_summary_plain(self, response: AgentResponse) -> None:
        status = "✓ 成功" if response.success else "✗ 失败"
        print(f"\n[响应状态] {status}")
        print(f"  总耗时: {response.total_latency_ms:.2f} ms")
        if response.time_cost_ms:
            print(f"  搜索耗时: {response.time_cost_ms} ms")
        if response.request_id:
            print(f"  RequestId: {response.request_id}")
        if response.log_id:
            print(f"  LogId: {response.log_id}")
        if response.usage:
            print(f"  Token 使用:")
            print(f"    输入: {response.usage.get('PromptTokens', '?')}")
            print(f"    输出: {response.usage.get('CompletionTokens', '?')}")
            print(f"    总计: {response.usage.get('TotalTokens', '?')}")
        if not response.success:
            print(f"  错误码: {response.error_code}")
            print(f"  错误信息: {response.error_message}")

    def render_references(self, response: AgentResponse) -> None:
        if not response.success or not response.search_results:
            return

        if _RICH_AVAILABLE:
            self._render_references_rich(response.search_results)
        else:
            self._render_references_plain(response.search_results)

    def _render_references_rich(self, results: List[Dict[str, Any]]) -> None:
        table = Table(
            title="📚 参考资料",
            box=box.ROUNDED,
            border_style="blue",
            show_lines=True,
        )
        table.add_column("#", style="cyan", width=4, justify="center")
        table.add_column("标题", style="bold", width=35)
        table.add_column("站点", style="green", width=18)
        table.add_column("权威度", style="magenta", width=10)
        table.add_column("摘要", style="white", width=50)

        for i, item in enumerate(results, 1):
            title = item.get("Title", "(无标题)")
            site = item.get("SiteName", "")
            auth = item.get("AuthInfoDes", "")
            summary = item.get("Summary", item.get("Snippet", ""))

            if len(title) > 35:
                title = title[:32] + "..."
            if len(summary) > 150:
                summary = summary[:147] + "..."

            table.add_row(str(i), title, site, auth, summary)

        self.console.print(table)

    def _render_references_plain(self, results: List[Dict[str, Any]]) -> None:
        print(f"\n参考资料 ({len(results)} 条):")
        print("-" * 60)
        for i, item in enumerate(results, 1):
            title = item.get("Title", "(无标题)")
            site = item.get("SiteName", "")
            url = item.get("Url", "")
            auth = item.get("AuthInfoDes", "")
            summary = item.get("Summary", item.get("Snippet", ""))
            print(f"\n[{i}] {title}")
            print(f"    站点: {site} | 权威度: {auth}")
            if url:
                print(f"    链接: {url}")
            print(f"    摘要: {summary[:200]}")

    def render_raw_json(self, response: AgentResponse) -> None:
        raw_data = {
            "answer": response.answer,
            "result_count": response.result_count,
            "search_results": response.search_results,
            "usage": response.usage,
            "card_results": response.card_results,
        }
        if _RICH_AVAILABLE:
            self.console.print("\n[bold]📄 完整数据:[/bold]")
            self.console.print(RichJSON(raw_data, indent=2))
        else:
            print("\n完整数据:")
            print(json.dumps(raw_data, indent=2, ensure_ascii=False))


def get_api_key() -> str:
    api_key = os.environ.get(API_KEY_ENV_VAR, "")
    if not api_key:
        logger.error(
            f"未找到 API Key。请设置环境变量 {API_KEY_ENV_VAR}，例如:\n"
            f"  export {API_KEY_ENV_VAR}=your_api_key_here\n"
            f"或在 .env 文件中配置。\n"
            f"\n注意：联网问答 Agent 使用独立的 API Key，需在「联网问答Agent」控制台创建。"
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


def parse_config(config_str: str) -> Dict[str, Any]:
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
        logger.error(f"config JSON 解析失败: {e}")
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
        description="联网问答 Agent API 测试脚本 - 搜索 + 大模型总结一体化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基础问答
  python agent_search_demo.py --query "今天北京天气怎么样"

  # 指定返回条数和时间范围
  python agent_search_demo.py --query "最新 AI 技术趋势" \\
      --config '{"Count": 15, "TimeRange": "OneMonth"}'

  # 指定站点范围搜索
  python agent_search_demo.py --query "Python教程" \\
      --config '{"Filter": {"Sites": "zhihu.com|jianshu.com"}}'

  # 非流式输出（一次性返回）
  python agent_search_demo.py --query "test" --no-stream

  # 输出原始 JSON 数据
  python agent_search_demo.py --query "test" --raw

  # 详细日志模式
  python agent_search_demo.py --query "test" -v
        """,
    )
    parser.add_argument(
        "--query",
        "-q",
        type=str,
        required=True,
        help="搜索/提问关键词 (1~100 字符)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="",
        help="自定义配置参数 (JSON 字符串或文件路径)",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="禁用流式输出，一次性返回结果",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="输出原始 JSON 响应数据",
    )
    parser.add_argument(
        "--no-references",
        action="store_true",
        help="不显示参考资料",
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

    config = parse_config(args.config)

    renderer = AgentResultRenderer()
    renderer.render_header(args.query, config)

    try:
        client = AgentSearchClient(api_key)
    except ValueError as e:
        logger.error(f"初始化客户端失败: {e}")
        sys.exit(1)

    if args.no_stream:
        response = client.ask(args.query, config)
        if response.success:
            print("\n" + "=" * 60)
            print("💡 AI 回答:")
            print("=" * 60)
            print(response.answer)
    else:
        event_iterator = client.stream_ask(args.query, config)
        response = renderer.render_streaming(event_iterator)

    renderer.render_response_summary(response)

    if response.success and not args.no_references:
        renderer.render_references(response)

    if args.raw:
        renderer.render_raw_json(response)

    sys.exit(0 if response.success else 1)


if __name__ == "__main__":
    main()
