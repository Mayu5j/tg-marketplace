import asyncio
import logging
import json
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, status
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.client.default import DefaultBotProperties
from redis.asyncio import Redis

# Scheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Database Engine
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# Imports from custom modular layers
from marketplace_bot.config import settings
from marketplace_bot.models import Base, OrderStatus, AccountStatus, PaymentMethod
from marketplace_bot.repositories import (
    UserRepository, RegionRepository, AccountRepository, OrderRepository, AdminLogRepository
)
from marketplace_bot.telethon_worker import get_telethon_manager, set_auth_code_callback
from marketplace_bot.payment_services import TonWatcherService, CryptoBotClient
from marketplace_bot.bot_handlers import user_router
from marketplace_bot.admin_panel import admin_router

# Core loggers
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("main_runner")

# Database Async Pool
engine = create_async_engine(settings.database_url_async, echo=False, pool_pre_ping=True)
async_session_pool = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Global Telethon worker manager
# Callback receives login codes from telethon and broadcasts it to the buyer via aiogram Bot
async def on_auth_code_intercepted(order_id: int, phone: str, otp_code: str):
    logger.info(f"BROADCAST OTP CODE: Order {order_id}, phone {phone}, OTP is {otp_code}")
    try:
        # Use a temporary session inside callback to send message
        bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
        async with async_session_pool() as session:
            order_repo = OrderRepository(session)
            order = await order_repo.get_by_id(order_id)
            if order and order.status == OrderStatus.PAID:
                # Forward to user
                success_msg = (
                    f"🔔 <b>КОД АВТОРИЗАЦИИ ПЕРЕХВАЧЕН!</b>\n\n"
                    f"📱 Для номера: <code>{phone}</code>\n"
                    f"🔑 Код входа: <b>{otp_code}</b>\n\n"
                    f"Введи этот код в приложении Telegram для завершения авторизации. "
                    f"После успешного входа аккаунт передан вам полностью."
                )
                await bot.send_message(chat_id=order.user_id, text=success_msg)
                
                # Mark account as SOLD and remove from available warehouse
                order.account.status = AccountStatus.SOLD
                await session.commit()
                logger.info(f"Order {order_id} marked as fully SOLD of phone {phone}")
    except Exception as e:
        logger.error(f"Error in on_auth_code_intercepted delivery: {e}")


set_auth_code_callback(on_auth_code_intercepted)
telethon_manager = get_telethon_manager()


# BACKGROUND JOB 1: Automatic cleanup of expired unpaid reservations
async def job_expire_orders():
    logger.info("Executing background task: job_expire_orders")
    async with async_session_pool() as session:
        order_repo = OrderRepository(session)
        try:
            cancelled_ids = await order_repo.cancel_expired_orders()
            if cancelled_ids:
                await session.commit()
                logger.info(f"Successfully cancelled expired reservations: {cancelled_ids}")
        except Exception as e:
            await session.rollback()
            logger.error(f"Error executing cancel_expired_orders Job: {e}")


# BACKGROUND JOB 2: Telethon Worker Cleanup scheduler
async def job_security_cleanup():
    logger.info("Executing background task: job_security_cleanup (Telethon sessions validation)")
    async with async_session_pool() as session:
        account_repo = AccountRepository(session)
        accounts_to_clean = await account_repo.fetch_accounts_for_cleanup()
        
        for acc in accounts_to_clean:
            # Process cleanup
            success, message = await telethon_manager.execute_security_cleanup(
                phone=acc.phone,
                api_id=acc.api_id,
                api_hash=acc.api_hash,
                encrypted_session=acc.encrypted_session
            )
            
            acc.cleanup_attempts += 1
            acc.last_cleanup_at = datetime.datetime.utcnow()
            acc.error_message = message if not success else None
            
            if success:
                acc.status = AccountStatus.AVAILABLE
                logger.info(f"Account {acc.phone} successfully passed cleanup and flag is set to AVAILABLE.")
            else:
                logger.warning(f"Cleanup failed for {acc.phone}, attempt {acc.cleanup_attempts}: {message}. Will retry in 30 minutes.")
                
            await session.commit()


# BACKGROUND JOB 3: Watch TON transaction comments
async def job_ton_wallet_watcher():
    logger.info("Executing background task: job_ton_wallet_watcher")
    watcher = TonWatcherService()
    transactions = await watcher.fetch_recent_wallet_transactions()
    
    if not transactions:
        logger.debug("No transactions fetched from TON wallet")
        return

    logger.info(f"TON Watcher: Processing {len(transactions)} transactions")
    async with async_session_pool() as session:
        order_repo = OrderRepository(session)
        for raw_tx in transactions:
            parsed = watcher.parse_transaction(raw_tx)
            if not parsed:
                logger.debug(f"TON Watcher: Failed to parse transaction: {raw_tx.get('transaction_id', {}).get('hash', 'unknown')}")
                continue
                
            comment = parsed["comment"]
            amount = parsed["amount"]
            tx_hash = parsed["tx_hash"]
            
            logger.debug(f"TON Watcher: TX {tx_hash[:16]}... | Amount: {amount} TON | Comment: '{comment}'")
            
            if comment and comment.startswith("MKP_"):
                # Potential marketplace payment matches
                logger.info(f"TON Watcher: Found MKP comment: '{comment}'")
                order = await order_repo.get_by_ton_comment(comment)
                if not order:
                    logger.warning(f"TON Watcher: No order found for comment '{comment}' in PENDING status")
                    continue
                    
                logger.info(f"TON Watcher: Order {order.id} found, checking amount")
                # Double-check invoice expected coin amounts
                # Scale price of TON or verify rates
                if not settings.TON_USDT_RATE or settings.TON_USDT_RATE <= 0:
                    logger.error(
                        f"TON Watcher: TON_USDT_RATE not configured or invalid! "
                        f"Set TON_USDT_RATE in .env (e.g., TON_USDT_RATE=7.5 means 1 TON = 7.5 USDT). "
                        f"Current value: {settings.TON_USDT_RATE}"
                    )
                    continue  # Skip payment matching until rate is configured
                
                expected_amount = order.price / settings.TON_USDT_RATE
                
                amount_diff = abs(amount - expected_amount)
                logger.info(f"TON Watcher: Amount check: received={amount} TON, expected={expected_amount} TON (price={order.price} USDT / rate={settings.TON_USDT_RATE}), diff={amount_diff}")
                
                if amount_diff < 0.05: # Allow small margin (0.05 TON for fees)
                    logger.info(f"Payment MATCHED! Order ID: {order.id}, Tx: {tx_hash}")
                    # Apply payment
                    await order_repo.apply_successful_payment(order.id, tx_hash=tx_hash, asset="TON")
                    await session.commit()
                    try:
                        await telethon_manager.start_login_code_interception(
                            phone=order.account.phone,
                            api_id=order.account.api_id,
                            api_hash=order.account.api_hash,
                            encrypted_session=order.account.encrypted_session,
                            order_id=order.id
                        )
                    except Exception as e:
                        logger.error(f"Failed to start code interception for TON order {order.id}: {e}")
                    
                    # Deliver keys & notify
                    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
                    # Forward user to start active listening
                    await bot.send_message(
                        chat_id=order.user_id,
                        text=f"✅ <b>Оплата в TON успешно зачислена!</b>\n"
                             f"ID транзакции: <code>{tx_hash}</code>\n\n"
                             f"Бот уже слушает входящие сообщения этого аккаунта.\n"
                             f"Откройте Telegram и запросите код — он придет сюда автоматически."
                    )
                else:
                    logger.warning(f"TON Watcher: Amount mismatch for order {order.id}: diff={amount_diff} TON (>0.05)")
            elif comment:
                logger.debug(f"TON Watcher: Comment does not start with MKP_: '{comment}'")
            else:
                logger.debug("TON Watcher: Transaction has no comment")


# Dependency middleware injection for aiogram FSM Context and sessions
async def db_session_middleware(handler, event, data):
    async with async_session_pool() as session:
        data["db_session"] = session
        data["user_repo"] = UserRepository(session)
        data["region_repo"] = RegionRepository(session)
        data["account_repo"] = AccountRepository(session)
        data["order_repo"] = OrderRepository(session)
        data["admin_log_repo"] = AdminLogRepository(session)
        try:
            res = await handler(event, data)
            await session.commit()
            return res
        except Exception as e:
            await session.rollback()
            raise e


# FastAPI Lifespan controls
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 0. Validate TON configuration
    if settings.TON_WALLET_ADDRESS and settings.TON_WALLET_ADDRESS != "EQC...YOUR_TELEGRAM_TON_WALLET":
        if not settings.TON_USDT_RATE or settings.TON_USDT_RATE <= 0:
            logger.warning(
                "⚠️  TON WALLET is configured but TON_USDT_RATE is missing or invalid! "
                "TON payments will NOT work until you set TON_USDT_RATE in .env. "
                "Example: TON_USDT_RATE=7.5 (meaning 1 TON = 7.5 USDT). "
                "Get current rate from: https://stonfi.app or similar"
            )
    
    # 1. Database migration/creation for rapid deploy setup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("SqlAlchemy database schemas loaded on engine.")

    # 2. Redis Initializer for FSM Storage
    redis_instance = Redis.from_url(settings.redis_url)
    storage = RedisStorage(redis_instance)

    # 3. aiogram Dispatcher
    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=storage)
    
    # Apply Middlewares
    dp.update.outer_middleware(db_session_middleware)
    
    # Register Routers
    dp.include_router(user_router)
    dp.include_router(admin_router)

    # 4. Background Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(job_expire_orders, "interval", seconds=60)
    scheduler.add_job(job_security_cleanup, "interval", minutes=30)
    scheduler.add_job(job_ton_wallet_watcher, "interval", seconds=30)
    scheduler.start()
    logger.info("Background jobs scheduler started.")

    # Execute long polling for Bot in a background Task (or setup webhook)
    bot_task = asyncio.create_task(dp.start_polling(bot))

    yield

    # Graceful shutdown Sequence
    bot_task.cancel()
    scheduler.shutdown()
    await redis_instance.close()
    await engine.dispose()
    logger.info("Application shut down cleanly.")


app = FastAPI(lifespan=lifespan, title="Telegram Accounts Marketplace Bot Core")


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "service": "Telegram Marketplace Engine"}


@app.post("/api/payments/cryptobot-webhook")
async def cryptobot_webhook_endpoint(request: Request, response: Response):
    """
    Acts as the target webhook validation endpoint for CryptoBot pay callbacks.
    When an invoice changes to paid, verifies contents and applies order complete actions in database.
    """
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8")
    
    # Process signature
    client = CryptoBotClient()
    signature = request.headers.get("crypto-pay-api-signature", "")
    
    # Log webhook transaction
    async with async_session_pool() as session:
        log_payload = PaymentLogRepository(session)
        await log_payload.log_webhook("cryptobot", body_str)
        await session.commit()

    if not await client.verify_signature(body_str, signature):
        response.status_code = status.HTTP_403_FORBIDDEN
        return {"error": "Invalid Signature Token"}

    try:
        data = json.loads(body_str)
        update_type = data.get("update_type")
        payload = data.get("payload", {})
        
        # We handle "invoice_paid" webhook type
        if update_type == "invoice_paid":
            invoice_id = str(payload.get("invoice_id"))
            tx_hash = payload.get("hash")
            asset = payload.get("asset")

            async with async_session_pool() as session:
                order_repo = OrderRepository(session)
                order = await order_repo.get_by_invoice_id(invoice_id)
                if order and order.status == OrderStatus.PENDING:
                    # Complete order and account states
                    await order_repo.apply_successful_payment(order.id, tx_hash=tx_hash, asset=asset)
                    await session.commit()
                    try:
                        await telethon_manager.start_login_code_interception(
                            phone=order.account.phone,
                            api_id=order.account.api_id,
                            api_hash=order.account.api_hash,
                            encrypted_session=order.account.encrypted_session,
                            order_id=order.id
                        )
                    except Exception as e:
                        logger.error(f"Failed to start code interception for CryptoBot order {order.id}: {e}")
                    
                    # Send prompt to start code intercepting process
                    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
                    await bot.send_message(
                        chat_id=order.user_id,
                        text=f"💰 <b>Ваш счет CryptoBot успешно оплачен!</b>\n\n"
                             f"📱 Номер готов: <code>{order.account.phone}</code>\n\n"
                             f"Бот уже слушает входящие сообщения этого аккаунта.\n"
                             f"Перейдите в приложение, запросите код — он придет в этот чат автоматически."
                    )
                    
    except Exception as e:
        logger.error(f"Error handling CryptoBot Webhook Payload: {e}")
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"error": str(e)}

    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("marketplace_bot.main:app", host=settings.API_HOST, port=settings.API_PORT, reload=False)
