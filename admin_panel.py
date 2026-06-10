import csv
import html
import io
import logging
import re
from typing import List, Optional, Dict, Any
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Document
from aiogram.filters import Command, Filter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest
from telethon.tl.functions.auth import ResetAuthorizationsRequest
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from marketplace_bot.config import settings
from marketplace_bot.models import AccountStatus, OrderStatus, Account, Order, Payment
from marketplace_bot.repositories import AccountRepository, RegionRepository, UserRepository, AdminLogRepository
from marketplace_bot.telethon_worker import TelegramSessionEncryptor

logger = logging.getLogger("admin_panel")

admin_router = Router()

# Temporary in-memory storage for admin logins: { admin_id: { "client": TelegramClient, ... } }
active_login_clients: Dict[int, Dict[str, Any]] = {}


# Custom security filter to restrict command execution only to authentic bot operators
class IsAdmin(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in settings.ADMIN_IDS


class AdminStates(StatesGroup):
    menu = State()
    adding_account_phone = State()
    adding_account_api_id = State()
    adding_account_api_hash = State()
    adding_account_price = State()
    adding_account_price_stars = State()
    adding_account_code = State()
    adding_account_2fa = State()
    adding_account_choose_listing = State()
    changing_price_id = State()
    changing_price_val = State()
    waiting_for_bulk_file = State()


# Get admin dashboard keyboard
def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="admin_add_acc"),
            InlineKeyboardButton(text="📥 Массовый импорт (.CSV)", callback_data="admin_bulk_import")
        ],
        [
            InlineKeyboardButton(text="🏷 Изменить цену", callback_data="admin_change_price"),
            InlineKeyboardButton(text="📋 Список всех номеров", callback_data="admin_list_accounts")
        ],
        [
            InlineKeyboardButton(text="📊 Статистика продаж", callback_data="admin_stats")
        ]
    ])


@admin_router.message(Command("admin"), IsAdmin())
async def cmd_admin_dashboard(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🛠 *Панель администратора Telegram Marketplace*\n\n"
        "Выберите необходимое действие с базой данных:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.menu)


@admin_router.callback_query(F.data == "admin_stats", IsAdmin())
async def show_admin_stats(
    callback: CallbackQuery, 
    user_repo: Optional[UserRepository] = None, 
    account_repo: Optional[AccountRepository] = None,
    **kwargs
):
    if user_repo is None:
        user_repo = kwargs.get("user_repo")
    if account_repo is None:
        account_repo = kwargs.get("account_repo")
    db_session = kwargs.get("db_session")
    if db_session:
        if user_repo is None:
            user_repo = UserRepository(db_session)
        if account_repo is None:
            account_repo = AccountRepository(db_session)
    if not user_repo or not account_repo:
        logger.error("Failed to resolve user_repo or account_repo for admin stats")
        await callback.message.answer("❌ Внутренняя ошибка: Не удалось инициализировать репозитории баз данных.")
        await callback.answer()
        return

    # Fetch database stats
    users_info = await user_repo.get_stats()

    status_counts_res = await account_repo.session.execute(
        select(Account.status, func.count(Account.id)).group_by(Account.status)
    )
    status_counts = {row[0]: row[1] for row in status_counts_res.all()}
    available_qty = status_counts.get(AccountStatus.AVAILABLE, 0)
    pending_qty = status_counts.get(AccountStatus.PENDING_CLEANUP, 0)
    sold_qty = status_counts.get(AccountStatus.SOLD, 0)

    paid_orders_res = await account_repo.session.execute(
        select(func.count(Order.id)).where(Order.status == OrderStatus.PAID)
    )
    paid_orders = paid_orders_res.scalar() or 0
    revenue_res = await account_repo.session.execute(
        select(func.coalesce(func.sum(Payment.amount), 0.0))
    )
    revenue_total = revenue_res.scalar() or 0.0

    stats_text = (
        f"📊 *Метрики и Статистика Магазина*\n\n"
        f"👥 Зарегистрировано покупателей: *{users_info.get('total_users', 0)}*\n"
        f"✅ Доступно для продажи номеров: *{available_qty}*\n"
        f"⏳ Ожидают очистку активных сессий: *{pending_qty}*\n\n"
        f"🧾 Продано аккаунтов: *{sold_qty}*\n"
        f"💳 Оплаченных заказов: *{paid_orders}*\n"
        f"💰 Выручка: *{revenue_total} USDT*\n\n"
        f"Все операции по Telethon сессиям логируются в Docker контейнере."
    )
    
    await callback.message.answer(stats_text, parse_mode="Markdown", reply_markup=get_admin_keyboard())
    await callback.answer()


def extract_country_code(phone: str) -> str:
    # Try to match common patterns
    for prefix in ["380", "998", "375", "996", "994", "370", "371", "372", "420", "44"]:
        if phone.startswith(prefix):
            return f"+{prefix}"
    if phone.startswith("7"):
        return "+7"
    if phone.startswith("1"):
        return "+1"
    if len(phone) > 3:
        return f"+{phone[:3]}"
    return "+7"


def get_flag_emoji(country_code: str) -> str:
    flags = {
        "+7": "🇷🇺",
        "+380": "🇺🇦",
        "+998": "🇺🇿",
        "+1": "🇺🇸",
        "+44": "🇬🇧",
        "+375": "🇧🇾",
        "+996": "🇰🇬",
        "+994": "🇦🇿",
        "+370": "🇱🇹",
        "+371": "🇱🇻",
        "+372": "🇪🇪",
        "+420": "🇨🇿"
    }
    return flags.get(country_code, "🏳️")


# --- ADD ACCOUNT FLOW ---
@admin_router.callback_query(F.data == "admin_add_acc", IsAdmin())
async def admin_add_account_init(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📞 Введите номер мобильного телефона аккаунта (в международном формате без +):")
    await state.set_state(AdminStates.adding_account_phone)
    await callback.answer()


@admin_router.message(AdminStates.adding_account_phone, IsAdmin())
async def admin_add_account_phone_received(message: Message, state: FSMContext):
    phone = message.text.strip().replace("+", "")
    if not phone.isdigit():
        await message.answer("❌ Ошибка: Номер телефона должен состоять только из цифр. Пожалуйста, введите номер еще раз:")
        return

    await state.update_data(new_phone=phone)
    await message.answer("📝 Введите API ID для этого аккаунта (целое число):")
    await state.set_state(AdminStates.adding_account_api_id)


@admin_router.message(AdminStates.adding_account_api_id, IsAdmin())
async def admin_add_account_api_id_received(message: Message, state: FSMContext):
    api_id_text = message.text.strip()
    try:
        api_id = int(api_id_text)
    except ValueError:
        await message.answer("❌ Ошибка: API ID должен быть целым числом. Пожалуйста, введите корректный API ID:")
        return

    await state.update_data(api_id=api_id)
    await message.answer("🔑 Введите API HASH для этого аккаунта:")
    await state.set_state(AdminStates.adding_account_api_hash)


@admin_router.message(AdminStates.adding_account_api_hash, IsAdmin())
async def admin_add_account_api_hash_received(message: Message, state: FSMContext):
    api_hash = message.text.strip()
    if not api_hash:
        await message.answer("❌ API HASH не может быть пустым. Введите его:")
        return

    await state.update_data(api_hash=api_hash)
    await message.answer("💵 Укажите цену для этого аккаунта в USDT (например, 7.5):")
    await state.set_state(AdminStates.adding_account_price)


@admin_router.message(AdminStates.adding_account_price, IsAdmin())
async def admin_add_account_price_received(
    message: Message, state: FSMContext
):
    price_text = message.text.strip().replace(",", ".")
    try:
        price = float(price_text)
    except ValueError:
        await message.answer("❌ Ошибка: Цена должна быть числом (например, 7.5). Пожалуйста, введите корректную цену:")
        return

    await state.update_data(price=price)
    await message.answer("⭐️ Укажите цену для этого аккаунта в Telegram Stars (например, 350):")
    await state.set_state(AdminStates.adding_account_price_stars)


@admin_router.message(AdminStates.adding_account_price_stars, IsAdmin())
async def admin_add_account_price_stars_received(
    message: Message, state: FSMContext, 
    account_repo: Optional[AccountRepository] = None, 
    region_repo: Optional[RegionRepository] = None, 
    admin_log_repo: Optional[AdminLogRepository] = None,
    **kwargs
):
    if account_repo is None:
        account_repo = kwargs.get("account_repo")
    if region_repo is None:
        region_repo = kwargs.get("region_repo")
    if admin_log_repo is None:
        admin_log_repo = kwargs.get("admin_log_repo")
    db_session = kwargs.get("db_session")
    if db_session:
        if account_repo is None:
            account_repo = AccountRepository(db_session)
        if region_repo is None:
            region_repo = RegionRepository(db_session)
        if admin_log_repo is None:
            admin_log_repo = AdminLogRepository(db_session)
    if not account_repo or not region_repo or not admin_log_repo:
        logger.error("Failed to resolve required repositories (account_repo, region_repo, admin_log_repo)")
        await message.answer("❌ Внутренняя ошибка сервера: Не удалось инициализировать репозитории для работы с базой данных.")
        return

    price_stars_text = message.text.strip()
    try:
        price_stars = int(price_stars_text)
    except ValueError:
        await message.answer("❌ Ошибка: Цена в звездах должна быть целым числом (например, 350). Пожалуйста, введите корректную цену:")
        return

    user_state_data = await state.get_data()
    phone = user_state_data.get("new_phone")
    api_id = user_state_data.get("api_id")
    api_hash = user_state_data.get("api_hash")
    price = user_state_data.get("price")

    await message.answer("⏳ Подключаемся к серверам Telegram для отправки кода...")

    # Create Telethon client with StringSession
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
    except Exception as e:
        logger.error(f"Failed to connect for {phone}: {e}")
        await message.answer(f"❌ Ошибка подключения к Telegram: {html.escape(str(e))}")
        return

    try:
        logger.info(f"Sending login code request to +{phone}")
        sent_code = await client.send_code_request(phone)
        phone_code_hash = sent_code.phone_code_hash
    except Exception as e:
        logger.error(f"Failed to send code for {phone}: {e}")
        await message.answer(f"❌ Ошибка отправки кода: {html.escape(str(e))}\nПожалуйста, убедитесь в правильности введенных данных.")
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    # Save active login state
    active_login_clients[message.from_user.id] = {
        "client": client,
        "phone_code_hash": phone_code_hash,
        "phone": phone,
        "api_id": api_id,
        "api_hash": api_hash,
        "price": price,
        "price_stars": price_stars
    }

    await message.answer(
        f"📩 Код авторизации отправлен на номер `+{phone}`!\n\n"
        f"Пожалуйста, введите код подтверждения из Telegram в этот чат:",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.adding_account_code)


@admin_router.message(AdminStates.adding_account_code, IsAdmin())
async def admin_add_account_code_received(
    message: Message, state: FSMContext,
    account_repo: Optional[AccountRepository] = None,
    region_repo: Optional[RegionRepository] = None,
    admin_log_repo: Optional[AdminLogRepository] = None,
    **kwargs
):
    if account_repo is None:
        account_repo = kwargs.get("account_repo")
    if region_repo is None:
        region_repo = kwargs.get("region_repo")
    if admin_log_repo is None:
        admin_log_repo = kwargs.get("admin_log_repo")
    db_session = kwargs.get("db_session")
    if db_session:
        if account_repo is None:
            account_repo = AccountRepository(db_session)
        if region_repo is None:
            region_repo = RegionRepository(db_session)
        if admin_log_repo is None:
            admin_log_repo = AdminLogRepository(db_session)

    if not account_repo or not region_repo or not admin_log_repo:
        logger.error("Failed to resolve repositories for code submission")
        await message.answer("❌ Внутренняя ошибка сервера: не удалось получить службы БД.")
        return

    code = message.text.strip().replace(" ", "")
    login_data = active_login_clients.get(message.from_user.id)
    if not login_data:
        await message.answer("❌ Сессия авторизации не найдена в памяти. Попробуйте добавить аккаунт заново.")
        await state.clear()
        return

    client = login_data["client"]
    phone_code_hash = login_data["phone_code_hash"]
    phone = login_data["phone"]

    await message.answer("⏳ Проверяем код авторизации...")

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        await finalize_admin_account_login(message, state, login_data, account_repo, region_repo, admin_log_repo)
    except SessionPasswordNeededError:
        await message.answer(
            "🔑 На аккаунте включена двухфакторная защита (2FA)!\n\n"
            "Пожалуйста, введите ваш облачный 2FA-пароль в чат:"
        )
        await state.set_state(AdminStates.adding_account_2fa)
    except Exception as e:
        logger.error(f"Sign-in code verification failed: {e}")
        await message.answer(f"❌ Ошибка входа: {html.escape(str(e))}\n\nПожалуйста, введите код повторно (или наберите /admin для отмены):")


@admin_router.message(AdminStates.adding_account_2fa, IsAdmin())
async def admin_add_account_2fa_received(
    message: Message, state: FSMContext,
    account_repo: Optional[AccountRepository] = None,
    region_repo: Optional[RegionRepository] = None,
    admin_log_repo: Optional[AdminLogRepository] = None,
    **kwargs
):
    if account_repo is None:
        account_repo = kwargs.get("account_repo")
    if region_repo is None:
        region_repo = kwargs.get("region_repo")
    if admin_log_repo is None:
        admin_log_repo = kwargs.get("admin_log_repo")
    db_session = kwargs.get("db_session")
    if db_session:
        if account_repo is None:
            account_repo = AccountRepository(db_session)
        if region_repo is None:
            region_repo = RegionRepository(db_session)
        if admin_log_repo is None:
            admin_log_repo = AdminLogRepository(db_session)

    if not account_repo or not region_repo or not admin_log_repo:
        logger.error("Failed to resolve repositories for 2FA submission")
        await message.answer("❌ Внутренняя ошибка сервера: не удалось получить службы БД.")
        return

    password = message.text.strip()
    login_data = active_login_clients.get(message.from_user.id)
    if not login_data:
        await message.answer("❌ Сессия авторизации не найдена в памяти. Попробуйте добавить аккаунт заново.")
        await state.clear()
        return

    client = login_data["client"]
    phone = login_data["phone"]

    await message.answer("⏳ Проверяем облачный пароль...")

    try:
        await client.sign_in(password=password)
        await finalize_admin_account_login(message, state, login_data, account_repo, region_repo, admin_log_repo)
    except Exception as e:
        logger.error(f"2FA password verification failed: {e}")
        await message.answer(f"❌ Неверный 2FA-пароль: {html.escape(str(e))}\n\nПожалуйста, попробуйте ввести пароль повторно:")


async def finalize_admin_account_login(
    message: Message, state: FSMContext, login_data: dict,
    account_repo: AccountRepository, region_repo: RegionRepository,
    admin_log_repo: AdminLogRepository
):
    client = login_data["client"]
    phone = login_data["phone"]
    api_id = login_data["api_id"]
    api_hash = login_data["api_hash"]
    price = login_data["price"]
    price_stars = login_data.get("price_stars", int(price * 50))

    try:
        # Save string session format from Telethon which serves as portable format in database
        session_str = client.session.save()

        # Encrypt the session securely 
        encryptor = TelegramSessionEncryptor()
        encrypted_session = encryptor.encrypt_session(session_str)

        country_code = extract_country_code(phone)
        flag = get_flag_emoji(country_code)

        region = await region_repo.get_by_code(country_code)
        if not region:
            region = await region_repo.create_region(f"Country {country_code}", country_code, flag)

        # SECURITY: Instantly terminate all other device sessions in the newly authenticated account!
        cleanup_msg = "Устройства не обнаружены."
        cleanup_success = True
        try:
            authorizations_res = await client(GetAuthorizationsRequest())
            authorizations = authorizations_res.authorizations
            
            logger.info(f"Account {phone} has {len(authorizations)} active sessions at login.")

            if len(authorizations) > 1:
                logger.info(f"Terminating remaining authorized sessions on {phone}...")
                await client(ResetAuthorizationsRequest())
                cleanup_msg = f"Успешно завершено {len(authorizations) - 1} посторонних сеансов!"
            else:
                cleanup_msg = "Аккаунт уже чист (сторонние сессии отсутствуют)."
        except FloodWaitError as fwe:
            cleanup_success = False
            cleanup_msg = f"Сессии будут сброшены позже (Telegram FloodWait: {fwe.seconds}с)"
        except Exception as e:
            cleanup_success = False
            cleanup_msg = f"Не удалось сбросить другие сессии сразу: {e} (будет выполнено в фоне)"

        # Save extracted data in login_data to preserve it for callback decision
        login_data["encrypted_session"] = encrypted_session
        login_data["region_id"] = region.id
        login_data["cleanup_success"] = cleanup_success
        login_data["cleanup_msg"] = cleanup_msg

        # Offer choice
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Выложить сейчас", callback_data="admin_pub_now"),
                InlineKeyboardButton(text="⏳ Ждать очистки сессий", callback_data="admin_pub_wait")
            ]
        ])

        await message.answer(
            f"🔑 <b>Сессия аккаунта <code>+{phone}</code> успешно верифицирована!</b>\n\n"
            f"⚙️ Результат первичной очистки: <i>{html.escape(cleanup_msg)}</i>\n"
            f"🛡 Требуется фоновое ожидание: <b>{'Нет, сессии уже очищены' if cleanup_success else 'Да, есть сторонние сессии'}</b>\n\n"
            f"Выберите режим публикации аккаунта в маркетплейс:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.adding_account_choose_listing)

    except Exception as ex:
        logger.error(f"Error during finalize_admin_account_login: {ex}")
        await message.answer(f"❌ Ошибка при подготовке авторизации: {html.escape(str(ex))}")
        active_login_clients.pop(message.from_user.id, None)
        await state.clear()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@admin_router.callback_query(AdminStates.adding_account_choose_listing, F.data.startswith("admin_pub_"), IsAdmin())
async def admin_choose_listing_callback(
    callback: CallbackQuery,
    state: FSMContext,
    account_repo: Optional[AccountRepository] = None,
    region_repo: Optional[RegionRepository] = None,
    admin_log_repo: Optional[AdminLogRepository] = None,
    **kwargs
):
    if account_repo is None:
        account_repo = kwargs.get("account_repo")
    if region_repo is None:
        region_repo = kwargs.get("region_repo")
    if admin_log_repo is None:
        admin_log_repo = kwargs.get("admin_log_repo")
    db_session = kwargs.get("db_session")
    if db_session:
        if account_repo is None:
            account_repo = AccountRepository(db_session)
        if region_repo is None:
            region_repo = RegionRepository(db_session)
        if admin_log_repo is None:
            admin_log_repo = AdminLogRepository(db_session)

    if not account_repo or not region_repo or not admin_log_repo:
        logger.error("Failed to resolve repositories for choosing listing")
        await callback.message.answer("❌ Внутренняя ошибка сервера: не удалось получить службы БД.")
        await callback.answer()
        return

    login_data = active_login_clients.get(callback.from_user.id)
    if not login_data:
        await callback.message.answer("❌ Сессия авторизации не найдена в памяти. Попробуйте добавить аккаунт заново.")
        await callback.answer()
        await state.clear()
        return

    phone = login_data["phone"]
    api_id = login_data["api_id"]
    api_hash = login_data["api_hash"]
    price = login_data["price"]
    price_stars = login_data.get("price_stars", int(price * 50))
    encrypted_session = login_data["encrypted_session"]
    region_id = login_data["region_id"]
    cleanup_success = login_data["cleanup_success"]
    cleanup_msg = login_data["cleanup_msg"]

    decision = callback.data # "admin_pub_now" or "admin_pub_wait"

    # Set status based on decision
    from marketplace_bot.models import AccountStatus
    
    if decision == "admin_pub_now":
        status = AccountStatus.AVAILABLE
    else:
        # admin decided to wait (current behavior)
        if cleanup_success:
            status = AccountStatus.AVAILABLE
        else:
            status = AccountStatus.PENDING_CLEANUP

    try:
        # Save account in DB with chosen status
        account = await account_repo.add_account(
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
            encrypted_session=encrypted_session,
            price=price,
            price_stars=price_stars,
            region_id=region_id
        )

        account.status = status
        if not cleanup_success and decision == "admin_pub_wait":
            account.error_message = cleanup_msg

        await admin_log_repo.log_action(
            admin_id=callback.from_user.id,
            action="ADD_ACCOUNT",
            details=f"Authorized phone +{phone}. Status: {status.value}. Price: {price}. Decision: {decision}. Cleanup: {cleanup_msg}",
            admin_username=callback.from_user.username,
            admin_first_name=callback.from_user.first_name
        )

        # Clear inline keyboard on decision prompt
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.message.answer(
            f"✅ <b>Аккаунт <code>+{phone}</code> успешно добавлен в бот!</b>\n\n"
            f"⚙️ Статус публикации: <b>{status.value.upper()}</b>\n"
            f"🛡 Решение админа: <b>{'Выложить сразу 🚀' if decision == 'admin_pub_now' else 'Ждать очистки сессий ⏳'}</b>\n"
            f"📋 Результат очистки: {html.escape(cleanup_msg)}\n\n"
            f"Сессия успешно верифицирована и сохранена.",
            parse_mode="HTML"
        )
    except Exception as ex:
        logger.error(f"Database save error during finalize_admin_account_login callback: {ex}")
        await callback.message.answer(f"❌ База данных не сохранила запись (возможно номер уже был добавлен): {html.escape(str(ex))}")
    finally:
        active_login_clients.pop(callback.from_user.id, None)
        await state.clear()
        await callback.answer()


# --- BULK CSV IMPORT ---
@admin_router.callback_query(F.data == "admin_bulk_import", IsAdmin())
async def admin_bulk_import_init(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📥 *Массовый импорт базы аккаунтов*\n\n"
        "Отправьте CSV файл с разметкой. Первая строка обязана содержать заголовки:\n"
        "`phone,api_id,api_hash,session,country,price`\n\n"
        "Бот проверит записи и добавит их в очередь на очистку.",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_for_bulk_file)
    await callback.answer()


@admin_router.message(AdminStates.waiting_for_bulk_file, F.document, IsAdmin())
async def admin_bulk_import_process(
    message: Message, state: FSMContext, bot: Bot,
    account_repo: Optional[AccountRepository] = None, 
    region_repo: Optional[RegionRepository] = None, 
    admin_log_repo: Optional[AdminLogRepository] = None,
    **kwargs
):
    if account_repo is None:
        account_repo = kwargs.get("account_repo")
    if region_repo is None:
        region_repo = kwargs.get("region_repo")
    if admin_log_repo is None:
        admin_log_repo = kwargs.get("admin_log_repo")
    db_session = kwargs.get("db_session")
    if db_session:
        if account_repo is None:
            account_repo = AccountRepository(db_session)
        if region_repo is None:
            region_repo = RegionRepository(db_session)
        if admin_log_repo is None:
            admin_log_repo = AdminLogRepository(db_session)
    if not account_repo or not region_repo or not admin_log_repo:
        logger.error("Failed to resolve required repositories (account_repo, region_repo, admin_log_repo) for bulk import")
        await message.answer("❌ Внутренняя ошибка сервера: Не удалось инициализировать репозитории для работы с базой данных.")
        return

    document: Document = message.document
    if not document.file_name.endswith('.csv') and not document.file_name.endswith('.txt'):
        await message.answer("❌ Неподдерживаемый формат файла. Поддерживаются только таблицы .CSV или сырой .TXT")
        return

    # Download file input
    file_bytes = io.BytesIO()
    await bot.download(document, destination=file_bytes)
    file_bytes.seek(0)
    
    # Read CSV
    text_content = file_bytes.read().decode('utf-8')
    csv_reader = csv.DictReader(io.StringIO(text_content))
    
    success_count = 0
    failure_count = 0
    encryptor = TelegramSessionEncryptor()

    for idx, row in enumerate(csv_reader, 1):
        try:
            phone = row["phone"].strip().replace("+", "")
            api_id = int(row["api_id"].strip())
            api_hash = row["api_hash"].strip()
            session_str = row["session"].strip()
            country = row["country"].strip()  # Country code
            price = float(row["price"].strip())

            region = await region_repo.get_by_code(country)
            if not region:
                region = await region_repo.create_region(f"Region {country}", country, "🏳️")

            encrypted_session = encryptor.encrypt_session(session_str)
            await account_repo.add_account(
                phone=phone,
                api_id=api_id,
                api_hash=api_hash,
                encrypted_session=encrypted_session,
                price=price,
                region_id=region.id
            )
            success_count += 1
        except Exception as e:
            logger.warning(f"Failed parsing CSV line {idx}: {e}")
            failure_count += 1

    await admin_log_repo.log_action(
        admin_id=message.from_user.id,
        action="MASS_IMPORT",
        details=f"Successful imports: {success_count}, failed: {failure_count}",
        admin_username=message.from_user.username,
        admin_first_name=message.from_user.first_name
    )

    await message.answer(
        f"📊 *Результаты импорта реестра:*\n"
        f"✅ Успешно импортировано: *{success_count}*\n"
        f"❌ Ошибок импорта/дублирующих номеров: *{failure_count}*\n\n"
        f"Очередь сессий Telethon готова к очистке.",
        parse_mode="Markdown"
    )
    await state.clear()


# --- LIST ALL NUMBERS ---
@admin_router.callback_query(F.data == "admin_list_accounts", IsAdmin())
async def admin_list_accounts(
    callback: CallbackQuery, 
    account_repo: Optional[AccountRepository] = None,
    **kwargs
):
    if account_repo is None:
        account_repo = kwargs.get("account_repo")
    db_session = kwargs.get("db_session")
    if db_session:
        if account_repo is None:
            account_repo = AccountRepository(db_session)
    if not account_repo:
        logger.error("Failed to resolve account_repo for admin listing")
        await callback.message.answer("❌ Внутренняя ошибка: Не удалось инициализировать репозиторий аккаунтов.")
        await callback.answer()
        return

    result = await account_repo.session.execute(
        select(Account)
        .options(selectinload(Account.region))
        .order_by(desc(Account.created_at))
        .limit(30)
    )
    accounts = list(result.scalars().all())

    res = ["📋 *Последние 30 аккаунтов:*"]
    for acc in accounts:
        region_label = acc.region.flag if acc.region else "🏳️"
        res.append(
            f"• ID {acc.id} | `{acc.phone}` | {region_label} | "
            f"{acc.price} USDT / {acc.price_stars}⭐ | Status: *{acc.status.value}*"
        )

    if len(accounts) == 0:
        res.append("База пуста.")
        
    await callback.message.answer("\n".join(res), parse_mode="Markdown", reply_markup=get_admin_keyboard())
    await callback.answer()


# --- CHANGE PRICE ---
@admin_router.callback_query(F.data == "admin_change_price", IsAdmin())
async def admin_change_price_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🏷 Введите ID аккаунта или номер телефона для изменения цены.\n"
        "Примеры: `ID:123` или `+79991234567`",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.changing_price_id)
    await callback.answer()


@admin_router.message(AdminStates.changing_price_id, IsAdmin())
async def admin_change_price_account_received(
    message: Message,
    state: FSMContext,
    account_repo: Optional[AccountRepository] = None,
    **kwargs
):
    if account_repo is None:
        account_repo = kwargs.get("account_repo")
    db_session = kwargs.get("db_session")
    if db_session and account_repo is None:
        account_repo = AccountRepository(db_session)
    if not account_repo:
        logger.error("Failed to resolve account_repo for change price flow")
        await message.answer("❌ Внутренняя ошибка: Не удалось инициализировать репозиторий аккаунтов.")
        return

    raw = message.text.strip()
    account = None
    if raw.lower().startswith("id:") or raw.startswith("#"):
        try:
            account_id = int(re.sub(r"[^0-9]", "", raw))
        except ValueError:
            await message.answer("❌ Некорректный ID. Попробуйте снова.")
            return
        account = await account_repo.get_by_id(account_id)
    else:
        phone = raw.replace("+", "").replace(" ", "")
        if not phone.isdigit():
            await message.answer("❌ Номер должен содержать только цифры. Попробуйте снова.")
            return
        if len(phone) <= 6:
            try:
                account_id = int(phone)
            except ValueError:
                await message.answer("❌ Некорректный ID. Попробуйте снова.")
                return
            account = await account_repo.get_by_id(account_id)
        else:
            account = await account_repo.get_by_phone(phone)

    if not account:
        await message.answer("❌ Аккаунт не найден. Проверьте ID или номер и попробуйте снова.")
        return

    await state.update_data(target_account_id=account.id)
    await message.answer(
        f"Текущая цена: *{account.price} USDT* / *{account.price_stars}⭐*\n"
        "Введите новую цену в USDT. Можно указать цену в звездах через пробел, например: `7.5 350`",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.changing_price_val)


@admin_router.message(AdminStates.changing_price_val, IsAdmin())
async def admin_change_price_value_received(
    message: Message,
    state: FSMContext,
    account_repo: Optional[AccountRepository] = None,
    admin_log_repo: Optional[AdminLogRepository] = None,
    **kwargs
):
    if account_repo is None:
        account_repo = kwargs.get("account_repo")
    if admin_log_repo is None:
        admin_log_repo = kwargs.get("admin_log_repo")
    db_session = kwargs.get("db_session")
    if db_session:
        if account_repo is None:
            account_repo = AccountRepository(db_session)
        if admin_log_repo is None:
            admin_log_repo = AdminLogRepository(db_session)
    if not account_repo or not admin_log_repo:
        logger.error("Failed to resolve repositories for price update")
        await message.answer("❌ Внутренняя ошибка сервера: Не удалось инициализировать репозитории.")
        return

    raw = message.text.strip().replace(",", ".")
    parts = [p for p in re.split(r"[;\s]+", raw) if p]
    if not parts:
        await message.answer("❌ Укажите цену в формате `7.5` или `7.5 350`.")
        return

    try:
        price = float(parts[0])
        if price <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Цена должна быть положительным числом. Попробуйте снова.")
        return

    price_stars = int(price * 50)
    if len(parts) > 1:
        try:
            price_stars = int(float(parts[1]))
        except ValueError:
            await message.answer("❌ Цена в звездах должна быть целым числом. Попробуйте снова.")
            return

    data = await state.get_data()
    account_id = data.get("target_account_id")
    if not account_id:
        await message.answer("❌ Не удалось определить аккаунт. Запустите изменение цены заново.")
        await state.clear()
        return

    account = await account_repo.get_by_id(account_id)
    if not account:
        await message.answer("❌ Аккаунт не найден. Запустите изменение цены заново.")
        await state.clear()
        return

    account.price = price
    account.price_stars = price_stars
    await account_repo.session.flush()

    await admin_log_repo.log_action(
        admin_id=message.from_user.id,
        action="CHANGE_PRICE",
        details=f"Account {account.id} ({account.phone}) price updated to {price} USDT / {price_stars} stars",
        admin_username=message.from_user.username,
        admin_first_name=message.from_user.first_name
    )

    await message.answer(
        f"✅ Цена обновлена: *{account.price} USDT* / *{account.price_stars}⭐*",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard()
    )
    await state.clear()
