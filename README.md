# Telegram Marketplace Bot (Production-Ready Codebase)

A full-featured, modular, and secure Telegram accounts marketplace bot developed in Python 3.12 with **aiogram 3**, **Telethon**, **PostgreSQL** (SQLAlchemy Async), **Redis**, and **FastAPI**.

## Major Architectural Features

1. **Automated Security Cleanup & Validation**: 
   Every account added to the system is placed in a `PENDING_CLEANUP` state. A background Telethon worker connects, authenticates, and issues a request to terminate all active sessions on other devices. If limit restrictions prevent immediate completion, the scheduler schedules automated retries every 30 minutes until exactly one device (the bot) remains authorized. Only then is the account released for sale.
2. **Encrypted Database Storage**: 
   Telethon session strings are encrypted prior to database insertion using cryptography (Fernet AES). This prevents attackers from hijacking valid client sessions in case of SQL injection or database leaks.
3. **Atomic Account Locking (Zero Double-Sales)**:
   Concurrency conflicts (race conditions) are prevented by enclosing order creation in a PostgreSQL `SELECT ... FOR UPDATE` row lock. When user A requests an account, the DB row is locked, set instantly to `RESERVED` status, and only released back to `AVAILABLE` if the order expires unpaid.
4. **Triple-Gateway Payment Processing**:
   - **CryptoBot API**: Invoices created dynamically. Verification triggers either on polling callbacks or signed webhook endpoints.
   - **TON Keeper Wallets**: The system watches the public TON ledger transactions for comments identifying active Orders (Format: `MKP_{order_id}_{timestamp}`). Safe transaction confirmations occur instantly without relying on centralized intermediaries.
   - **Telegram Stars**: Supports modern @Stars payments with immediate pre-checkout verification schemas.
5. **Real-time SMS/Login Code Interception**:
   Once user purchases are cleared, a dedicated Telethon event handler listens for incoming OTP notifications from Telegram (User `777000`) and relays the login code in real-time directly to the active buyer.

## Repository Diagram

```
marketplace_bot/
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── config.py           # Base pydantic-settings environment schema
├── models.py           # SQLAlchemy declarative database tables
├── repositories.py     # Asynchronous database services (Repository Pattern)
├── telethon_worker.py  # Session encryption, cleanups, and OTP interceptor
├── payment_services.py # Billing controllers for CryptoBot, Stars, & TON Blockchain
├── bot_handlers.py     # End-user aiogram handlers and FSM States
├── admin_panel.py      # Operator tools, mass imports, and stock stats
└── main.py             # FastAPI webhook loop, APScheduler, and long-polling bot
```

## Quick Start (Production Execution)

1. Rename the configuration environment template:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` and fill out your `BOT_TOKEN`, `ADMIN_IDS` (comma-separated), and custom secret credentials.
3. Build and launch the containerized stack:
   ```bash
   docker-compose up --build -d
   ```
4. Access health metrics and diagnostic hooks:
   ```bash
   curl http://localhost:8000/api/health
   ```
