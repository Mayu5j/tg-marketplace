import enum
import datetime
from typing import List, Optional
from sqlalchemy import String, Integer, Float, ForeignKey, DateTime, BigInteger, Enum, Text, Index, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AccountStatus(enum.Enum):
    PENDING_CLEANUP = "pending_cleanup"  # Added, waiting for automatic security scanning
    AVAILABLE = "available"              # Fully clean, ready for purchase
    RESERVED = "reserved"                # Added to invoice/order, waiting for payment confirmation
    SOLD = "sold"                        # Purchased, login code processed, delivered
    BANNED = "banned"                    # Failed verification/ban detected by Telethon
    INVALID = "invalid"                  # Telethon session expired or key invalid


class OrderStatus(enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentMethod(enum.Enum):
    CRYPTO_BOT = "crypto_bot"
    TON_TRANSFER = "ton_transfer"
    TELEGRAM_STARS = "telegram_stars"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram User ID
    username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    first_name: Mapped[str] = mapped_column(String(150), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    total_spent: Mapped[float] = mapped_column(Float, default=0.0)

    orders: Mapped[List["Order"]] = relationship("Order", back_populates="user")
    admin_logs: Mapped[List["AdminLog"]] = relationship("AdminLog", back_populates="admin")


class Region(Base):
    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    country_code: Mapped[str] = mapped_column(String(10), unique=True)  # e.g., "+7", "+1", "+998"
    flag: Mapped[str] = mapped_column(String(10))  # Emoji like "🇷🇺", "🇺🇸"

    accounts: Mapped[List["Account"]] = relationship("Account", back_populates="region")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    api_id: Mapped[int] = mapped_column(Integer)
    api_hash: Mapped[str] = mapped_column(String(100))
    # Enrypted Telethon session string is stored in DB for privacy / anti-theft
    encrypted_session: Mapped[str] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Float, index=True)
    price_stars: Mapped[int] = mapped_column(Integer, default=0, index=True)
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus), default=AccountStatus.PENDING_CLEANUP, index=True
    )
    region_id: Mapped[int] = mapped_column(Integer, ForeignKey("regions.id"), index=True)
    
    # Scanning metadata
    cleanup_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_cleanup_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    region: Mapped[Region] = relationship("Region", back_populates="accounts")
    orders: Mapped[List["Order"]] = relationship("Order", back_populates="account")

    # Ensure phone has database index
    __table_args__ = (
        Index("idx_accounts_status_region", "status", "region_id"),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"), index=True)
    price: Mapped[float] = mapped_column(Float)
    price_stars: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.PENDING, index=True
    )
    payment_method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod))
    
    # Specific attributes for matching TON transfers or CryptoBot billing
    payment_invoice_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, unique=True, index=True)
    ton_comment: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    ton_expected_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ton_rate_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, index=True)
    paid_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship("User", back_populates="orders")
    account: Mapped[Account] = relationship("Account", back_populates="orders")
    payments: Mapped[List["Payment"]] = relationship("Payment", back_populates="order")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    asset: Mapped[str] = mapped_column(String(30))  # "TON", "USDT", "STARS"
    tx_hash: Mapped[Optional[str]] = mapped_column(String(150), nullable=True, unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    order: Mapped[Order] = relationship("Order", back_populates="payments")


class AdminLog(Base):
    __tablename__ = "admin_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100))  # "ADD_ACCOUNT", "MASS_IMPORT", "CHANGE_PRICE"
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    admin: Mapped[User] = relationship("User", back_populates="admin_logs")


class PaymentLog(Base):
    __tablename__ = "payment_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gateway: Mapped[str] = mapped_column(String(50))  # "cryptobot", "ton_watcher", "telegram_stars"
    payload: Mapped[str] = mapped_column(Text)  # JSON-stringified raw webhook info
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)