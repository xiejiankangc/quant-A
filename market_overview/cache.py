"""本地 JSONL 缓存：落盘约定与读取入口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Cache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _resolve(self, rel: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_rows(
        self,
        rel: str,
        rows: list[dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> Path:
        path = self._resolve(rel)
        body = "".join(
            f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows
        )
        path.write_text(body, encoding="utf-8", newline="\n")
        if meta is not None:
            meta_path = path.with_name(f"{path.name}.meta.json")
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        return path

    def read_rows(self, rel: str) -> list[dict[str, Any]]:
        path = self.root / rel
        if not path.is_file():
            raise FileNotFoundError(f"缺少缓存文件: {path}（先运行 fetch）")
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return rows

    def read_meta(self, rel: str) -> dict[str, Any] | None:
        path = self.root / rel
        meta_path = path.with_name(f"{path.name}.meta.json")
        if not meta_path.is_file():
            return None
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def write_json(self, rel: str, obj: dict[str, Any]) -> Path:
        path = self._resolve(rel)
        path.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return path

    def read_json(self, rel: str) -> dict[str, Any] | None:
        path = self.root / rel
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()
