import os
from pathlib import Path

TEST_DB = Path(__file__).parent / "test-stockbot.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["PUSHPLUS_TOKEN"] = ""
os.environ['HOUSING_LOOKUP_ACCESS_MODE'] = 'legacy'
os.environ['HOUSING_LOOKUP_SIGNING_SECRET'] = ''
os.environ['HOUSING_LOOKUP_API_URL'] = ''


def pytest_sessionfinish(session, exitstatus):
    from app.db import engine

    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{TEST_DB}{suffix}")
        if path.exists():
            path.unlink()
