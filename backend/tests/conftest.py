"""pytest 全局配置：在任何 app 导入之前把测试环境指向本地隔离资源。

- 业务库用临时 SQLite 文件（不依赖外部 MySQL 服务）
- Checkpoint 用内存模式（可按需覆盖）
"""
import os
import tempfile

_TEST_DIR = tempfile.mkdtemp(prefix="ai_general_plat_test_")

os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DIR}/test.db")
os.environ.setdefault("DATABASE_SYNC_URL", f"sqlite:///{_TEST_DIR}/test.db")
os.environ.setdefault("AGENT_CHECKPOINT_DSN", "memory")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("AGENT_FILES_ROOT", os.path.join(_TEST_DIR, "agent_files"))
