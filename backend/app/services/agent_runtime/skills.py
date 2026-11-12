"""
Skill 注册与装载（DeepAgents 原生 Skills 机制）。

Skill = 目录 + SKILL.md（YAML frontmatter: name/description，name 与目录名一致）。
StateBackend 下 skill 文件以 files 注入；FilesystemBackend 下由 SkillsMiddleware
从 backend 读取（source 路径 /skills/<name>/）。

Registry 支持启用/禁用（disabled_skills），来源=内置目录或平台配置目录。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from app.core.config import settings


@dataclass
class SkillMeta:
    name: str
    description: str
    version: str = "1.0"
    source: str = "builtin"
    enabled: bool = True
    path: str = ""


BUILTIN_SKILLS: Dict[str, Dict[str, str]] = {
    "chart-report": {
        "description": "生成数据图表与文字结合的分析小结（Markdown 表格 + 结论）。",
        "body": (
            "---\n"
            "name: chart-report\n"
            "description: 生成数据图表与文字结合的分析小结（Markdown 表格 + 结论）\n"
            "---\n\n"
            "# chart-report\n\n"
            "当需要输出数据分析小结时使用本技能：\n"
            "1. 先用表格列出关键指标；\n"
            "2. 给出 2-4 条结论；\n"
            "3. 结论必须与表格数字一致，不得编造。\n"
        ),
    },
    "meeting-minutes": {
        "description": "把原始素材整理为结构化会议纪要（议题/决议/待办）。",
        "body": (
            "---\n"
            "name: meeting-minutes\n"
            "description: 把原始素材整理为结构化会议纪要（议题/决议/待办）\n"
            "---\n\n"
            "# meeting-minutes\n\n"
            "当用户给出会议记录/录音转写素材时使用本技能：\n"
            "1. 提炼议题与讨论要点；\n"
            "2. 归纳决议；\n"
            "3. 输出待办清单（负责人 + 截止时间，如素材中没有则标注待定）。\n"
        ),
    },
}


class SkillRegistry:
    """技能注册表：内置技能 + 启用范围控制。"""

    def __init__(self, disabled: Optional[Set[str]] = None):
        self.disabled: Set[str] = disabled or set()

    @classmethod
    def from_settings(cls) -> "SkillRegistry":
        raw = getattr(settings, "AGENT_DISABLED_SKILLS", "") or ""
        disabled = {x.strip() for x in raw.split(",") if x.strip()}
        return cls(disabled=disabled)

    def list_skills(self) -> List[SkillMeta]:
        out = []
        for name, spec in BUILTIN_SKILLS.items():
            out.append(SkillMeta(
                name=name,
                description=spec["description"],
                enabled=name not in self.disabled,
            ))
        return out

    def enabled_skills(self) -> List[SkillMeta]:
        return [s for s in self.list_skills() if s.enabled]

    def files_payload(self) -> Dict[str, Dict[str, str]]:
        """StateBackend 注入格式：{"/skills/<name>/SKILL.md": {"content": ...}}"""
        from deepagents.backends.utils import create_file_data

        files: Dict[str, Any] = {}
        for meta in self.enabled_skills():
            body = BUILTIN_SKILLS[meta.name]["body"]
            files[f"/skills/{meta.name}/SKILL.md"] = create_file_data(content=body)
        return files
