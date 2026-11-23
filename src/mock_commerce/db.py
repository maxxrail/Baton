"""Settings and session factory for the mock store. Independent of `baton` on purpose:
the mock store stands in for a customer's real commerce backend."""

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


class MockCommerceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mock_commerce_database_url: str = "sqlite:///./mock_commerce.db"


def make_session_factory(url: str | None = None) -> sessionmaker[Session]:
    url = url or MockCommerceSettings().mock_commerce_database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    return sessionmaker(bind=engine, expire_on_commit=False)
