import asyncio
import base64
import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "chatgptimg"
DEFAULT_MODEL = "gpt-image-1536x1024"
DEFAULT_INSTRUCTIONS = "You are a helpful assistant."


class GPTImageError(Exception):
    pass


@dataclass
class GeneratedImage:
    image_bytes: bytes
    ext: str
    revised_prompt: str = ""


@register(
    PLUGIN_NAME,
    "xjf000",
    "调用 CLIProxy Responses API 的 GPT Image 生图插件",
    "1.0.0",
)
class ChatGPTImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def initialize(self):
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=True)

    @filter.command("gptimg", alias={"生图", "画图", "gimg"})
    async def gptimg(self, event: AstrMessageEvent, prompt: str = ""):
        """使用 GPT Image 模型生图。示例：/gptimg 画一张赛博朋克的香港，要有汉字"""
        prompt_text = self._resolve_prompt(prompt, event.message_str)
        if not prompt_text:
            yield event.plain_result(
                "请输入提示词，例如：/gptimg 画一张赛博朋克的香港，要有汉字"
            )
            return

        api_url = self._get_str_config("api_url")
        api_key = self._get_str_config("api_key")
        if not api_url or not api_key:
            yield event.plain_result("请先在插件配置中填写 api_url 和 api_key。")
            return

        yield event.plain_result("正在生成图片，请稍候...")

        try:
            generated = await self._generate_image(prompt_text)
            image_path = await self._save_image_bytes(generated.image_bytes, generated.ext)
        except GPTImageError as exc:
            logger.warning("chatgptimg generate failed: %s", exc)
            yield event.plain_result("生图失败：" + self._safe_error_message(exc))
            return
        except Exception as exc:
            logger.exception("chatgptimg unexpected error")
            yield event.plain_result("生图失败：" + self._safe_error_message(exc))
            return

        yield event.image_result(str(image_path))

        if self._get_bool_config("send_revised_prompt", False):
            revised_prompt = generated.revised_prompt or "接口未返回修订后的提示词。"
            yield event.plain_result("修订提示词：{}".format(revised_prompt))

    async def terminate(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _generate_image(self, prompt: str) -> GeneratedImage:
        if self._get_bool_config("stream", True):
            return await self._generate_image_from_stream(prompt)
        return await self._generate_image_from_json(prompt)

    async def _generate_image_from_stream(self, prompt: str) -> GeneratedImage:
        client = await self._get_client()
        body = self._build_request_body(prompt, stream=True)

        try:
            async with client.stream(
                "POST",
                self._get_str_config("api_url"),
                headers=self._build_headers(stream=True),
                json=body,
                timeout=self._build_timeout(),
            ) as response:
                await self._raise_for_error_status(response)

                content_type = response.headers.get("content-type", "").lower()
                if "text/event-stream" not in content_type:
                    raw = await response.aread()
                    return self._parse_generated_image_payload(
                        json.loads(raw.decode("utf-8", errors="ignore"))
                    )

                event_type: Optional[str] = None
                data_lines: List[str] = []
                generated: Optional[GeneratedImage] = None

                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        maybe_generated = self._flush_sse_event(event_type, data_lines)
                        if maybe_generated is not None:
                            generated = maybe_generated
                        data_lines = []
                        event_type = line[6:].strip()
                        continue

                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                        continue

                    if not line.strip():
                        maybe_generated = self._flush_sse_event(event_type, data_lines)
                        if maybe_generated is not None:
                            generated = maybe_generated
                        event_type = None
                        data_lines = []

                maybe_generated = self._flush_sse_event(event_type, data_lines)
                if maybe_generated is not None:
                    generated = maybe_generated

                if generated is None:
                    raise GPTImageError("接口返回成功，但没有拿到图片数据。")
                return generated
        except httpx.TimeoutException as exc:
            raise GPTImageError("请求超时，请调大 timeout_seconds 或稍后重试。") from exc
        except ValueError as exc:
            raise GPTImageError("接口返回的数据格式无法解析。") from exc

    async def _generate_image_from_json(self, prompt: str) -> GeneratedImage:
        client = await self._get_client()
        body = self._build_request_body(prompt, stream=False)

        try:
            response = await client.post(
                self._get_str_config("api_url"),
                headers=self._build_headers(stream=False),
                json=body,
                timeout=self._build_timeout(),
            )
            await self._raise_for_error_status(response)
            return self._parse_generated_image_payload(response.json())
        except httpx.TimeoutException as exc:
            raise GPTImageError("请求超时，请调大 timeout_seconds 或稍后重试。") from exc
        except ValueError as exc:
            raise GPTImageError("接口返回的不是合法 JSON。") from exc

    def _flush_sse_event(
        self, event_type: Optional[str], data_lines: List[str]
    ) -> Optional[GeneratedImage]:
        if not event_type or not data_lines:
            return None

        raw = "\n".join(data_lines).strip()
        if not raw or raw == "[DONE]":
            return None

        payload = json.loads(raw)

        if event_type in {"error", "response.failed"}:
            raise GPTImageError(self._extract_error_message(payload) or "接口返回失败。")

        if event_type == "response.output_item.done":
            item = payload.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "image_generation_call"
                and item.get("result")
            ):
                return self._parse_generated_image_item(item)
            return None

        if event_type == "response.completed":
            return self._parse_generated_image_payload(payload)

        return None

    def _parse_generated_image_payload(self, payload: Dict[str, Any]) -> GeneratedImage:
        direct_output = payload.get("output")
        if isinstance(direct_output, list):
            image_call = self._find_image_call(direct_output)
            if image_call is not None:
                return self._parse_generated_image_item(image_call)

        response_obj = payload.get("response")
        if isinstance(response_obj, dict):
            response_output = response_obj.get("output")
            if isinstance(response_output, list):
                image_call = self._find_image_call(response_output)
                if image_call is not None:
                    return self._parse_generated_image_item(image_call)

        error_message = self._extract_error_message(payload)
        if error_message:
            raise GPTImageError(error_message)

        raise GPTImageError("接口返回中未找到 image_generation_call 结果。")

    def _find_image_call(self, output_items: List[Any]) -> Optional[Dict[str, Any]]:
        for item in output_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image_generation_call" and item.get("result"):
                return item
        return None

    def _parse_generated_image_item(self, item: Dict[str, Any]) -> GeneratedImage:
        if item.get("type") != "image_generation_call":
            raise GPTImageError("接口未返回图片结果。")

        image_base64 = str(item.get("result", "")).strip()
        if not image_base64:
            raise GPTImageError("接口返回了 image_generation_call，但缺少 result 字段。")

        try:
            image_bytes = base64.b64decode(image_base64)
        except Exception as exc:
            raise GPTImageError("图片数据不是合法的 Base64。") from exc

        return GeneratedImage(
            image_bytes=image_bytes,
            ext=self._normalize_extension(
                item.get("output_format") or item.get("format") or "png"
            ),
            revised_prompt=str(item.get("revised_prompt") or "").strip(),
        )

    async def _save_image_bytes(self, image_bytes: bytes, ext: str) -> Path:
        output_dir = self._get_output_dir()
        filename = "gptimg-{}-{}.{}".format(
            datetime.now().strftime("%Y%m%d-%H%M%S"),
            uuid.uuid4().hex[:8],
            ext,
        )
        image_path = output_dir / filename
        await asyncio.to_thread(image_path.write_bytes, image_bytes)
        return image_path

    def _get_output_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            plugin_name = getattr(self, "name", PLUGIN_NAME) or PLUGIN_NAME
            output_dir = (
                Path(get_astrbot_data_path()) / "plugin_data" / plugin_name / "generated"
            )
        except Exception:
            output_dir = Path(tempfile.gettempdir()) / PLUGIN_NAME / "generated"

        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _build_request_body(self, prompt: str, stream: bool) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self._get_str_config("model", DEFAULT_MODEL),
            "instructions": self._get_str_config("instructions", DEFAULT_INSTRUCTIONS),
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "parallel_tool_calls": self._get_bool_config("parallel_tool_calls", True),
            "stream": stream,
            "store": self._get_bool_config("store", False),
        }

        reasoning: Dict[str, str] = {}
        reasoning_effort = self._get_str_config("reasoning_effort")
        reasoning_summary = self._get_str_config("reasoning_summary")
        if reasoning_effort:
            reasoning["effort"] = reasoning_effort
        if reasoning_summary:
            reasoning["summary"] = reasoning_summary
        if reasoning:
            body["reasoning"] = reasoning

        include_fields: List[str] = []
        if self._get_bool_config("include_reasoning_encrypted_content", False):
            include_fields.append("reasoning.encrypted_content")
        if include_fields:
            body["include"] = include_fields

        return body

    def _build_headers(self, stream: bool) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(self._get_str_config("api_key")),
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        return headers

    def _build_timeout(self) -> httpx.Timeout:
        timeout_seconds = self._get_int_config("timeout_seconds", 180)
        return httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 30))

    async def _raise_for_error_status(self, response: httpx.Response) -> None:
        if not response.is_error:
            return

        body_text = (await response.aread()).decode("utf-8", errors="ignore").strip()
        message = body_text

        try:
            payload = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError:
            payload = {}

        extracted_error = self._extract_error_message(payload)
        if extracted_error:
            message = extracted_error

        if not message:
            message = response.reason_phrase or "请求失败"

        raise GPTImageError("HTTP {}: {}".format(response.status_code, message[:500]))

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=True)
        return self._client

    def _extract_error_message(self, payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""

        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "code", "type"):
                value = error.get(key)
                if value:
                    return str(value).strip()
        elif error:
            return str(error).strip()

        response_obj = payload.get("response")
        if isinstance(response_obj, dict):
            response_error = response_obj.get("error")
            if isinstance(response_error, dict):
                for key in ("message", "code", "type"):
                    value = response_error.get(key)
                    if value:
                        return str(value).strip()
            elif response_error:
                return str(response_error).strip()

        message = payload.get("message")
        if message:
            return str(message).strip()

        return ""

    def _resolve_prompt(self, prompt: str, message_str: str) -> str:
        prompt_text = (prompt or "").strip()
        stripped_message = (message_str or "").strip()
        if not stripped_message:
            return prompt_text

        parts = stripped_message.split(maxsplit=1)
        command_name = parts[0].lstrip("/")
        if command_name in {"gptimg", "gimg", "生图", "画图"}:
            if len(parts) == 2:
                return parts[1].strip()
            return prompt_text

        return prompt_text or stripped_message

    def _normalize_extension(self, ext: Any) -> str:
        normalized = str(ext or "png").strip().lower().lstrip(".")
        if normalized in {"png", "jpg", "jpeg", "webp", "gif"}:
            return normalized
        return "png"

    def _get_str_config(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _get_bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _get_int_config(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    def _safe_error_message(self, exc: Exception) -> str:
        message = str(exc).strip()
        if not message:
            return exc.__class__.__name__
        return message[:300]
