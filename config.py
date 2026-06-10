import os
from typing import List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # General Bot Configurations
    BOT_TOKEN: str = Field(..., description="Telegram Bot Token from BotFather")
    ADMIN_IDS: List[int] = Field(default=[], description="List of Telegram user IDs with Admin access")
    
    # Server configuration
    API_HOST: str = Field(default="0.0.0.0", description="FastAPI bind host")
    API_PORT: int = Field(default=8000, description="FastAPI bind port")
    APP_URL: str = Field(default="https://my-app-domain.com", description="App external URL for Webhooks")

    # PostgreSQL Configuration
    POSTGRES_USER: str = Field(default="postgres")
    POSTGRES_PASSWORD: str = Field(default="postgres_secret")
    POSTGRES_DB: str = Field(default="marketplace_db")
    POSTGRES_HOST: str = Field(default="localhost")
    POSTGRES_PORT: int = Field(default=5432)

    @property
    def database_url_async(self) -> str:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def database_url_sync(self) -> str:
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    # Redis Config
    REDIS_HOST: str = Field(default="localhost")
    REDIS_PORT: int = Field(default=6379)
    REDIS_PASSWORD: Optional[str] = Field(default=None)

    @property
    def redis_url(self) -> str:
        auth_part = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth_part}{self.REDIS_HOST}:{self.REDIS_PORT}/0"

    # Encryption Key (32-bytes, base64-encoded for Fernet encrypting/decrypting Telethon session strings)
    ENCRYPTION_KEY: str = Field(
        default="gJr_vI6SXZmG1v2mG5Xm_i9M_zP6gY2R9X7l-mE9P_c=",
        description="Fernet key for encrypting Telethon sessions in database"
    )

    # Payment Gateways Setup
    CRYPTO_BOT_TOKEN: str = Field(default="CRYPTO_BOT_MOCK_TOKEN", description="Token from @CryptoBot")
    TON_WALLET_ADDRESS: str = Field(default="EQC...YOUR_TELEGRAM_TON_WALLET", description="Hot wallet address for incoming TON transfers")
    TON_API_KEY: Optional[str] = Field(default=None, description="Toncenter API key for transaction validation")
    TONCENTER_BASE_URL: str = Field(
        default="https://toncenter.com/api/v2/getTransactions",
        description="Toncenter API endpoint for getTransactions"
    )
    TON_RATE_API_URL: str = Field(
        default="https://tonapi.io/v2/rates",
        description="TON rate API endpoint used to quote TON invoices"
    )
    TONAPI_KEY: Optional[str] = Field(default=None, description="Optional TonAPI bearer token for rates")
    TON_USDT_RATE: Optional[float] = Field(
        default=None,
        description="Manual fallback rate: 1 TON = X USDT for TON transfer conversion"
    )

    # Order Reservation TTL (in seconds)
    ORDER_RESERVATION_TTL_SECS: int = Field(default=900, description="15 minutes order holding lock")


# Initialize settings instance
settings = Settings()
