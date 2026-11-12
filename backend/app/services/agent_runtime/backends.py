"""
虚拟文件后端：CompositeBackend 组合。

- 默认 StateBackend：会话内虚拟文件（随 checkpoint 保存，files 为 state 键）
- /workspace/ 前缀：FilesystemBackend 映射到 data/agent_files/{user}/{agent}/{session}
  virtual_mode=True，禁止指向宿主机根目录，max_file_size_mb 限制
"""
from __future__ import annotations

import os
from typing import Optional

from app.core.config import settings


def build_backend(user_id: int, agent_id: int, session_id: Optional[int]):
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    default_backend = StateBackend()

    base = getattr(settings, "AGENT_FILES_ROOT", "./data/agent_files")
    session_part = str(session_id) if session_id is not None else "ephemeral"
    root_dir = os.path.abspath(os.path.join(base, str(user_id), str(agent_id), session_part))
    os.makedirs(root_dir, exist_ok=True)

    fs_backend = FilesystemBackend(
        root_dir=root_dir,
        virtual_mode=True,
        max_file_size_mb=getattr(settings, "AGENT_MAX_FILE_SIZE_MB", 10),
    )

    return CompositeBackend(
        default=default_backend,
        routes={"/workspace/": fs_backend},
        artifacts_root="/",
    )


def workspace_root(user_id: int, agent_id: int, session_id: Optional[int]) -> str:
    base = getattr(settings, "AGENT_FILES_ROOT", "./data/agent_files")
    session_part = str(session_id) if session_id is not None else "ephemeral"
    return os.path.abspath(os.path.join(base, str(user_id), str(agent_id), session_part))
