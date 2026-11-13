"""深圳保租房/公租房通告监控。

该包与股票流水线完全隔离：独立的 SQLAlchemy metadata、独立的 CLI、
独立的表前缀，只复用同一个 SQLite engine 和 .env。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
