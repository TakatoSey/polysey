import os

# No real credentials or production database are used by the regression suite.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_NOT_REAL")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
