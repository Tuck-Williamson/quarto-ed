import os
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

_raw_url = os.environ.get("DATABASE_URL", "")


def _normalize_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("mysql://") and "+aiomysql" not in url:
        url = url.replace("mysql://", "mysql+aiomysql://", 1)
    return url


DATABASE_URL = _normalize_url(_raw_url)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=1800,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def get_db_session():
    async with AsyncSessionLocal() as session:
        yield session


_fernet = Fernet(os.environ["TOKEN_ENCRYPTION_KEY"].encode())


def encrypt_token(plain: str) -> bytes:
    return _fernet.encrypt(plain.encode())


def decrypt_token(ciphertext: bytes) -> str:
    return _fernet.decrypt(ciphertext).decode()
