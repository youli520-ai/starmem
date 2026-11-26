"""星忆（Starmem）Hermes memory provider —— S1 最小壳。

S1 只做两件事：
1. 让 Hermes 认识这个 provider（可挂可卸）
2. on_memory_write 收脏标记入本地队列，真正的入库由 memd 的文件 watcher 完成

prefetch 在 S3 接入，system_prompt_block 返回空（A 模式：内置 MEMORY.md 已注入）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import MemoryProvider, RecallStatus


class StarmemProvider(MemoryProvider):
    def _log(self, msg: str) -> None:
        try:
            from pathlib import Path

            p = Path("/opt/data/logs/starmem/hook.log")
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except Exception:
            pass

    def __init__(self) -> None:
        self._bank = "default"
        self._endpoint = ""
        self._token = ""
        self._home: Optional[Path] = None
        self._queue: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._last_count = 0
        self._warm_lock = threading.Lock()
        self._last_warm = 0.0

    @property
    def name(self) -> str:
        return "starmem-memory"

    def is_available(self) -> bool:
        # ABC 契约：只查配置/依赖，不联网
        if os.environ.get("STAREM_DISABLED") == "1":
            return False
        if os.environ.get("STAREM_ENDPOINT"):
            return True
        return Path("/opt/data/starmem/data/banks").is_dir()

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        home = Path(kwargs.get("hermes_home") or "/home/agent/.hermes")
        self._home = home
        name = home.name
        self._bank = "default" if name in (".hermes", "") else name
        self._endpoint = os.environ.get("STAREM_ENDPOINT", "http://127.0.0.1:9910").rstrip("/")
        self._token = os.environ.get("STAREM_TOKEN", "")
        self._log(f"initialize bank={self._bank} endpoint={self._endpoint}")
        threading.Thread(target=self._warm_endpoint, daemon=True).start()

    def system_prompt_block(self) -> str:
        return ""

    _TRIVIAL = {
        "继续", "好", "好的", "嗯", "行", "可以", "收到", "明白", "谢谢", "辛苦了",
        "在吗", "测试", "ok", "OK", "嗯嗯", "好嘞", "了解", "没事",
    }

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_count = 0  # 每轮重置，recall_status 只反映本轮
        q = (query or "").strip()[:800]
        if len(q) < 2 or q in self._TRIVIAL:
            return ""
        try:
            import urllib.request

            payload = json.dumps(
                {"profile": self._bank, "query": q, "budget_tokens": 400}
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            req = urllib.request.Request(
                self._endpoint + "/v1/recall", data=payload, headers=headers, method="POST"
            )
            result: dict[str, Any] = {}

            def _call() -> None:
                try:
                    result["raw"] = urllib.request.urlopen(req, timeout=1.5).read()
                except Exception as exc:
                    result["err"] = exc

            t = threading.Thread(target=_call, daemon=True)
            t.start()
            t.join(0.15)
            if t.is_alive():
                raise TimeoutError("prefetch 150ms deadline")
            if "err" in result:
                raise result["err"]
            data = json.loads(result["raw"])
        except Exception as exc:
            self._log(f"prefetch failed: {type(exc).__name__}")
            # 冷启或 memd 刚重启：立刻补一次预热，下一轮恢复
            if type(exc).__name__ == "TimeoutError":
                threading.Thread(target=self._warm_endpoint, daemon=True).start()
            return ""
        items = data.get("items") or []
        self._last_count = len(items)
        if not items:
            return ""
        lines = ["[星忆] 相关记忆："]
        for it in items:
            title = (it.get("title") or "").strip()
            snip = (it.get("snippet") or "").strip().replace("\n", " ")
            lines.append(f"- {title}：{snip[:100]}")
        self._log(f"prefetch injected {len(items)} tokens={data.get('tokens')}")
        return "\n".join(lines)

    def _warm_endpoint(self) -> None:
        with self._warm_lock:
            if time.time() - self._last_warm < 30:
                return
            self._last_warm = time.time()
            try:
                import urllib.request

                headers = {"Content-Type": "application/json"}
                if self._token:
                    headers["Authorization"] = f"Bearer {self._token}"
                req = urllib.request.Request(
                    self._endpoint + "/v1/warm", data=b"{}", headers=headers, method="POST"
                )
                urllib.request.urlopen(req, timeout=15.0).read()
                self._log("warm ok")
            except Exception as exc:
                self._log(f"warm failed: {type(exc).__name__}")

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # 只预热模型/索引，不发召回、不落 recall_log
        threading.Thread(target=self._warm_endpoint, daemon=True).start()

    def _warm(self, query: str) -> None:
        try:
            import urllib.request

            payload = json.dumps(
                {"profile": self._bank, "query": query, "budget_tokens": 400}
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            req = urllib.request.Request(
                self._endpoint + "/v1/recall", data=payload, headers=headers, method="POST"
            )
            urllib.request.urlopen(req, timeout=5.0).read()
        except Exception:
            pass

    def recall_status(self) -> Optional[RecallStatus]:
        n = self._last_count
        return RecallStatus(provider_label="星忆", count=n) if n else None

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        # 钩子载荷已脱敏，不能当 hash 来源；只记脏标记，watcher 读磁盘原文入库
        with self._lock:
            self._queue.append({"action": action, "target": target})
            if len(self._queue) > 200:
                del self._queue[:100]
        # 快路径：后台通知 memd 重扫该 bank 的 L0，超时不阻塞
        self._log(f"on_memory_write {action} {target}")
        threading.Thread(target=self._notify, args=(action, target), daemon=True).start()

    def _notify(self, action: str, target: str) -> None:
        try:
            import urllib.request

            payload = json.dumps(
                {"profile": self._bank, "action": action, "target": target}
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            req = urllib.request.Request(
                self._endpoint + "/v1/retain", data=payload, headers=headers, method="POST"
            )
            urllib.request.urlopen(req, timeout=0.8).read()
        except Exception:
            pass

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        # 后台投递，不阻塞 Hermes 的 sync worker
        self._log(f"sync_turn session={session_id} user={len(user_content or '')} assistant={len(assistant_content or '')}")
        threading.Thread(
            target=self._post_turn,
            args=(user_content or "", assistant_content or "", session_id),
            daemon=True,
        ).start()

    def _post_turn(self, user: str, assistant: str, session_id: str) -> None:
        try:
            import urllib.request

            turn_id = hashlib.sha256(
                f"{session_id}|{user[:200]}|{assistant[:200]}".encode("utf-8")
            ).hexdigest()[:16]
            payload = json.dumps(
                {
                    "profile": self._bank,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "user": user[:4000],
                    "assistant": assistant[:4000],
                }
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            req = urllib.request.Request(
                self._endpoint + "/v1/turn", data=payload, headers=headers, method="POST"
            )
            urllib.request.urlopen(req, timeout=2.0).read()
            self._log(f"turn posted {turn_id}")
        except Exception as exc:
            self._log(f"turn post failed: {type(exc).__name__}: {exc}")

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        return json.dumps({"error": "not_implemented", "stage": "S3"})


def register(ctx: Any) -> None:
    ctx.register_memory_provider(StarmemProvider())
