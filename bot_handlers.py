import datetime
import json
import logging
from typing import Optional
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from marketplace_bot.config import settings
from marketplace_bot.models import PaymentMethod, AccountStatus, OrderStatus
from marketplace_bot.repositories import (
    UserRepository, RegionRepository, AccountRepository, OrderRepository
)
from marketplace_bot.payment_services import CryptoBotClient, TelegramStarsPayments, TonWatcherService
from marketplace_bot.telethon_worker import get_telethon_manager

logger = logging.getLogger("bot_handlers")

user_router = Router()


# FSM States
class BotStates(StatesGroup):
    main_menu = State()
    selecting_region = State()
    browsing_catalog = State()
    paying_invoice = State()
    waiting_for_login_initiation = State()
    monitoring_login_process = State()


# Keyboards Helpers
def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛒 Купить аккаунт"), KeyboardButton(text="📞 Все доступные номера")],
            [KeyboardButton(text="🎛 Сортировать по цене"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="💬 Поддержка")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )


def get_sorting_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💵 Дешевле → Дороже 📈", callback_data="sort_price_asc"),
            InlineKeyboardButton(text="💵 Дороже → Дешевле 📉", callback_data="sort_price_desc")
        ]
    ])


@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user_repo: UserRepository):
    await state.clear()
    await user_repo.get_or_create(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
    
    welcome_text = (
        f"🤖 <b>Добро пожаловать в Telegram Marketplace!</b>\n\n"
        f"Здесь вы можете приобрести чистые, проверенные Telegram аккаунты "
        f"с гарантией полной безопасности. Все сторонние сессии автоматически "
        f"очищаются перед выгрузкой в наш каталог.\n\n"
        f"Выберите необходимый пункт в меню:"
    )
    
    await message.answer(
        welcome_text,
        reply_markup=get_main_menu_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(BotStates.main_menu)


# --- CATEGORY / REGION DISPLAY ---
@user_router.message(F.text == "🛒 Купить аккаунт")
async def show_regions(message: Message, state: FSMContext, region_repo: RegionRepository):
    regions = await region_repo.get_all()
    if not regions:
        # Create some default regions for database setup demo if empty
        r1 = await region_repo.create_region("USA", "+1", "🇺🇸")
        r2 = await region_repo.create_region("UK", "+44", "🇬🇧")
        r3 = await region_repo.create_region("Russia", "+7", "🇷🇺")
        r4 = await region_repo.create_region("Uzbekistan", "+998", "🇺🇿")
        regions = [r1, r2, r3, r4]

    buttons = []
    # Build a clean dynamic list of regions with emojis
    for r in regions:
        buttons.append([InlineKeyboardButton(
            text=f"{r.flag} {r.name} ({r.country_code})",
            callback_data=f"region_{r.id}"
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        "🌍 <b>Выберите интересующий регион / страну аккаунтов:</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(BotStates.selecting_region)


# --- LISTING ACCOUNTS ---
@user_router.callback_query(F.data.startswith("region_"))
async def handle_region_selection(callback: CallbackQuery, state: FSMContext, account_repo: AccountRepository):
    region_id = int(callback.data.split("_")[1])
    await state.update_data(current_region_id=region_id)
    
    # List available accounts
    accounts = await account_repo.list_available_for_purchase(region_id=region_id)
    if not accounts:
        await callback.message.answer("😔 В данной локации пока нет готовых к продаже номеров. Загляните позже!")
        await callback.answer()
        return

    # Build available catalog cards
    for account in accounts[:10]:  # Limit output cards for clean UI
        msg = (
            f"📦 <b>Карточка аккаунта</b>\n"
            f"📱 Номер: <code>{account.phone}</code>\n"
            f"🌍 Страна: {account.region.flag} {account.region.name}\n"
            f"💵 Цена: <b>{account.price} USDT</b> (или <b>{account.price_stars} ⭐️</b>)\n"
            f"🔒 Статус: Verified Security"
        )
        
        buy_btn = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡️ Купить", callback_data=f"buy_acc_{account.id}"),
                InlineKeyboardButton(text="❌ Назад", callback_data="back_to_regions")
            ]
        ])
        
        await callback.message.answer(msg, reply_markup=buy_btn, parse_mode="HTML")
    
    await callback.answer()
    await state.set_state(BotStates.browsing_catalog)


@user_router.callback_query(F.data == "back_to_regions")
async def back_to_regions(callback: CallbackQuery, state: FSMContext, region_repo: RegionRepository):
    regions = await region_repo.get_all()
    buttons = [[InlineKeyboardButton(text=f"{r.flag} {r.name} ({r.country_code})", callback_data=f"region_{r.id}")] for r in regions]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("🌍 <b>Выберите интересующий регион / страну аккаунтов:</b>", reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(BotStates.selecting_region)
    await callback.answer()


# --- SORTING ---
@user_router.message(F.text == "🎛 Сортировать по цене")
async def show_sorting_menu(message: Message):
    await message.answer(
        "⚡️ Выберите направление сортировки для всего каталога номеров:",
        reply_markup=get_sorting_keyboard()
    )


@user_router.callback_query(F.data.startswith("sort_"))
async def list_sorted_accounts(callback: CallbackQuery, account_repo: AccountRepository):
    sort_dir = True if callback.data == "sort_price_desc" else False
    accounts = await account_repo.list_available_for_purchase(sort_price_desc=sort_dir)
    
    if not accounts:
        await callback.message.answer("😔 База пуста. Доступных номеров не найдено.")
        await callback.answer()
        return

    await callback.message.answer(f"📈 <b>Показ отсортированных аккаунтов ({'убывание' if sort_dir else 'возрастание'} цены):</b>", parse_mode="HTML")
    
    for account in accounts[:5]:  # show top 5 matching items to keep chat history clean
        msg = (
            f"📦 <b>Карточка аккаунта</b>\n"
            f"📱 Номер: <code>{account.phone}</code>\n"
            f"🌍 Страна: {account.region.flag} {account.region.name}\n"
            f"💵 Цена: <b>{account.price} USDT</b>\n"
        )
        buy_btn = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚡️ Купить", callback_data=f"buy_acc_{account.id}")
        ]])
        await callback.message.answer(msg, reply_markup=buy_btn, parse_mode="HTML")
        
    await callback.answer()


# --- ALL AVAILABLE NUMBERS LIST ---
@user_router.message(F.text == "📞 Все доступные номера")
async def show_all_available_num(message: Message, account_repo: AccountRepository):
    accounts = await account_repo.list_available_for_purchase()
    if not accounts:
        await message.answer("😔 Извините, в данный момент все проверенные аккаунты распроданы.")
        return

    res_list = [f"📋 <b>Все доступные номера в продаже:</b>"]
    for idx, acc in enumerate(accounts, 1):
        res_list.append(f"{idx}. <code>{acc.phone}</code> - {acc.region.flag} - <b>{acc.price} USDT</b> (ID: {acc.id})")
    
    await message.answer("\n".join(res_list), parse_mode="HTML")


# --- USER PROFILE ---
@user_router.message(F.text == "👤 Профиль")
async def handle_profile(message: Message, user_repo: UserRepository):
    user = await user_repo.get_or_create(message.from_user.id)
    profile_text = (
        f"👤 <b>Ваш профиль в магазине</b>\n\n"
        f"🆔 Ваш ID: <code>{user.id}</code>\n"
        f"💰 Всего потрачено: <b>{user.total_spent} USDT</b>\n"
        f"📅 С нами с: {user.created_at.strftime('%d.%m.%Y')}\n\n"
        f"Все купленные у нас сеансы защищены. Входные OTP-коды отправляются в этот чат автоматически."
    )
    await message.answer(profile_text, parse_mode="HTML")


# --- SUPPORT ---
@user_router.message(F.text == "💬 Поддержка")
async def handle_support(message: Message):
    support_reply = (
        "🛠 <b>Служба техподдержки клиентов</b>\n\n"
        "Если у вас возникла проблема с покупкой, выходом других сессий "
        "или получением кода безопасности, пожалуйста, свяжитесь с нашим админом:\n"
        "👤 Контакт: @telegram_marketplace_support\n\n"
        "⏰ График работы оператора: 24/7"
    )
    await message.answer(support_reply, parse_mode="HTML")


# --- INITIATE PURCHASE ---
@user_router.callback_query(F.data.startswith("buy_acc_"))
async def process_buy_account(callback: CallbackQuery, state: FSMContext, order_repo: OrderRepository, account_repo: AccountRepository, user_repo: UserRepository):
    account_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    await user_repo.get_or_create(
        tg_id=user_id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name
    )
    
    # Verify account is still available before proceeding
    account = await account_repo.get_by_id(account_id)
    if not account:
        await callback.message.answer("❌ Извините, но этот аккаунт уже куплен другим пользователем. Выберите другой номер.")
        await callback.answer()
        return

    if account.status != AccountStatus.AVAILABLE:
        await callback.message.answer("❌ Извините, но этот аккаунт сейчас недоступен для покупки. Выберите другой номер.")
        await callback.answer()
        return

    # Allow user to pick payment client
    await state.update_data(target_account_id=account_id)
    
    pay_methods_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 CryptoBot (USDT/TON)", callback_data="pay_method_cryptobot"),
            InlineKeyboardButton(text="💎 TON Wallet Keeper Direct", callback_data="pay_method_ton")
        ],
        [
            InlineKeyboardButton(text="⭐️ Telegram Stars Payment", callback_data="pay_method_stars")
        ],
        [
            InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_purchase")
        ]
    ])
    
    await callback.message.answer(
        "💳 <b>Выберите удобный способ оплаты:</b>",
        reply_markup=pay_methods_keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@user_router.callback_query(F.data == "cancel_purchase")
async def cancel_purchase(callback: CallbackQuery, state: FSMContext, order_repo: OrderRepository):
    data = await state.get_data()
    order_id = data.get("current_order_id")
    if order_id:
        cancelled = await order_repo.cancel_order(order_id)
        if cancelled:
            await callback.message.edit_text("❌ Заказ отменен. Выбранный номер снова доступен для покупки.")
        else:
            await callback.message.edit_text("❌ Не удалось отменить заказ. Возможно, он уже оплачен или истек.")
    else:
        await callback.message.edit_text("❌ Покупка отменена. Возврат в главное меню.")

    await state.set_state(BotStates.main_menu)
    await callback.answer()


@user_router.callback_query(F.data.startswith("cancel_active_order_"))
async def cancel_active_order(callback: CallbackQuery, state: FSMContext, order_repo: OrderRepository):
    order_id = int(callback.data.split("_")[3])
    cancelled = await order_repo.cancel_order(order_id)
    
    if cancelled:
        await callback.message.answer("❌ Заказ отменен. Выбранный номер снова доступен в каталоге для покупки.")
        try:
            await callback.message.delete()
        except Exception:
            pass
    else:
        await callback.message.answer("❌ Не удалось отменить заказ (возможно, он уже оплачен или время вышло).")
        
    await state.set_state(BotStates.main_menu)
    await callback.answer()


@user_router.callback_query(F.data.startswith("pay_method_"))
async def trigger_order_invoice_creation(callback: CallbackQuery, state: FSMContext, order_repo: OrderRepository):
    method_str = callback.data.split("_")[2]
    user_data = await state.get_data()
    account_id = user_data.get("target_account_id")
    
    if not account_id:
        await callback.message.answer("❌ Ошибка заказа. Пожалуйста, запустите выбор заново.")
        await callback.answer()
        return
        
    method_mapping = {
        "cryptobot": PaymentMethod.CRYPTO_BOT,
        "ton": PaymentMethod.TON_TRANSFER,
        "stars": PaymentMethod.TELEGRAM_STARS
    }
    payment_method = method_mapping[method_str]
    
    ton_rate_usdt = None
    if payment_method == PaymentMethod.TON_TRANSFER:
        ton_rate_usdt = await TonWatcherService().fetch_ton_usdt_rate()
        if not ton_rate_usdt or ton_rate_usdt <= 0:
            await callback.message.answer(
                "❌ Не удалось получить актуальный курс TON. Попробуйте позже или выберите другой способ оплаты."
            )
            await callback.answer()
            return

    try:
        # Atomic lock and create reservation structure
        order = await order_repo.create_invoice_with_lock(
            user_id=callback.from_user.id,
            account_id=account_id,
            payment_method=payment_method,
            ttl_seconds=settings.ORDER_RESERVATION_TTL_SECS,
            ton_rate_usdt=ton_rate_usdt
        )
    except Exception as e:
        logger.error(f"Order create validation error: {e}")
        await callback.message.answer("❌ Извините, но этот аккаунт уже куплен другим пользователем. Выберите другой номер.")
        await callback.answer()
        return

    # Process specific pay mechanisms
    if payment_method == PaymentMethod.CRYPTO_BOT:
        # Generate official CryptoBot Invoice Link
        client = CryptoBotClient()
        crypto_invoice = await client.create_invoice(
            amount=order.price,
            asset="USDT",
            description=f"Payment for TG Account Order ID: {order.id}"
        )
        if not crypto_invoice:
            await order_repo.cancel_order(order.id)
            await callback.message.answer(
                "❌ Не удалось создать счет в CryptoBot. Попробуйте позже или выберите другой способ оплаты."
            )
            await callback.answer()
            return

        invoice_url = crypto_invoice.get("pay_url")
        if not invoice_url:
            await order_repo.cancel_order(order.id)
            await callback.message.answer(
                "❌ CryptoBot не вернул ссылку на оплату. Попробуйте позже или выберите другой способ оплаты."
            )
            await callback.answer()
            return

        # Bind invoice ID to the order for webhook events matching
        order.payment_invoice_id = str(crypto_invoice.get("invoice_id"))
        await order_repo.session.flush()

        kbd = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Оплатить в CryptoBot", url=invoice_url)],
            [
                InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"verify_order_{order.id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_active_order_{order.id}")
            ]
        ])
        
        instructions = (
            f"🛒 <b>Счет #{order.id} создан!</b>\n\n"
            f"💵 К оплате: <b>{order.price} USDT</b>\n"
            f"📱 Покупаемый номер: <code>{order.account.phone}</code>\n"
            f"⏰ Срок резервирования: <b>15 минут</b>\n\n"
            f"После совершения перевода, нажмите кнопку верификации ниже:"
        )
        await callback.message.answer(instructions, reply_markup=kbd, parse_mode="HTML")

    elif payment_method == PaymentMethod.TON_TRANSFER:
        # TON Keeper Direct matching memo. The amount is fixed on the order at invoice creation time.
        ton_amount = order.ton_expected_amount
        amount_line = (
            f"Отправьте ровно <b>{ton_amount:.3f} TON</b> "
            f"(цена аккаунта: {order.price} USDT, курс заказа: 1 TON = {order.ton_rate_usdt:.4f} USDT)"
        )
        instructions = (
            f"💎 <b>Инструкция оплаты через TON Wallet Transfer</b>\n\n"
            f"{amount_line} на наш горячий кошелек:\n"
            f"<code>{settings.TON_WALLET_ADDRESS}</code>\n\n"
            f"⚠️ <b>ВАЖНО:</b> Укажите этот уникальный комментарий при отправке транзакции, иначе бот не сможет зафиксировать платеж:\n"
            f"<code>{order.ton_comment}</code>\n\n"
            f"Бот мониторит сеть TON блокчейна каждые 30 сек. Заказ будет засчитан только если "
            f"по этому комментарию придет ровно указанная сумма TON."
        )
        kbd = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"verify_order_{order.id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_active_order_{order.id}")
            ]
        ])
        await callback.message.answer(instructions, reply_markup=kbd, parse_mode="HTML")

    elif payment_method == PaymentMethod.TELEGRAM_STARS:
        # Generate native Stars invoice
        stars_invoice = TelegramStarsPayments.get_stars_invoice(
            order_id=order.id,
            price=order.price_stars, # Use the specific stars price!
            description=f"Telegram Account {order.account.phone} activation keys"
        )
        
        bot = callback.bot
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=stars_invoice["title"],
            description=stars_invoice["description"],
            payload=stars_invoice["payload"],
            provider_token=stars_invoice["provider_token"],
            currency=stars_invoice["currency"],
            prices=[LabeledPrice(label="Purchasing phone", amount=stars_invoice["prices"][0]["amount"])],
            start_parameter=stars_invoice["start_parameter"]
        )

        cancel_kbd = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_active_order_{order.id}")]
        ])
        await callback.message.answer(
            "⏳ Оплатите счет в звездах выше. Если вы передумали, вы можете отменить покупку и освободить номер:",
            reply_markup=cancel_kbd
        )

    await state.set_state(BotStates.paying_invoice)
    await state.update_data(current_order_id=order.id)
    await callback.answer()


# --- PRE_CHECKOUT HANDLER FOR TELEGRAM STARS ---
@user_router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_q: PreCheckoutQuery, bot: Bot):
    # Safe to approve all checkout invoices, as we locked the account structure during invoicing
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


# --- STARS SUCCESS DISPATCHER ---
@user_router.message(F.successful_payment)
async def process_stars_successful_payment(message: Message, state: FSMContext, order_repo: OrderRepository):
    payload_str = message.successful_payment.invoice_payload
    payload = json.loads(payload_str)
    order_id = payload.get("order_id")

    order, account = await order_repo.apply_successful_payment(
        order_id=order_id,
        tx_hash=message.successful_payment.telegram_payment_charge_id,
        asset="STARS"
    )
    
    await database_commit_and_deliver_auth_keys(message, state, order, account)


# --- MANUAL VERIFICATION FALLBACK FOR OTHER CHANNELS ---
@user_router.callback_query(F.data.startswith("verify_order_"))
async def manual_verification_callback(callback: CallbackQuery, state: FSMContext, order_repo: OrderRepository):
    order_id = int(callback.data.split("_")[2])
    order = await order_repo.get_by_id(order_id)
    
    if not order:
        await callback.message.answer("❌ Счет не обнаружен.")
        await callback.answer()
        return
        
    if order.status == OrderStatus.PAID:
        await database_commit_and_deliver_auth_keys(callback.message, state, order, order.account)
    else:
        if order.payment_method == PaymentMethod.CRYPTO_BOT and order.payment_invoice_id:
            logger.info(f"Checking CryptoBot invoice: {order.payment_invoice_id}")
            client = CryptoBotClient()
            invoice = await client.get_invoice(order.payment_invoice_id)
            if invoice:
                logger.info(f"CryptoBot invoice status: {invoice.get('status')}, id: {invoice.get('invoice_id')}")
            if invoice and invoice.get("status") == "paid":
                await order_repo.apply_successful_payment(
                    order_id=order.id,
                    tx_hash=invoice.get("hash"),
                    asset=invoice.get("asset") or "USDT"
                )
                await database_commit_and_deliver_auth_keys(callback.message, state, order, order.account)
                await callback.answer()
                return

        logger.info(f"Checking status of billing id: {order.id}")
        await callback.answer("⏳ Транзакция еще не обнаружена на блокчейне или API. Пожалуйста, повторите верификацию через 30-60 сек.")


async def database_commit_and_deliver_auth_keys(message: Message, state: FSMContext, order, account):
    """
    Called upon payment receipt to initiate Telethon worker session forwarding.
    """
    await state.clear()

    interception_notice = ""
    try:
        manager = get_telethon_manager()
        await manager.start_login_code_interception(
            phone=account.phone,
            api_id=account.api_id,
            api_hash=account.api_hash,
            encrypted_session=account.encrypted_session,
            order_id=order.id
        )
        interception_notice = "✅ Бот уже слушает входящие коды этого аккаунта."
    except Exception as e:
        logger.error(f"Failed to start code interception for order {order.id}: {e}")
        interception_notice = "⚠️ Не удалось запустить перехват автоматически. Если код не приходит, обратитесь в поддержку."
    
    notification = (
        f"🎉 <b>ОПЛАТА УСПЕШНО ПРИНЯТА!</b>\n\n"
        f"📱 Вы приобрели номер: <code>{account.phone}</code>\n"
        f"🔐 Доступный сеанс зарезервирован только под вас.\n\n"
        f"🚩 <b>ИНСТРУКЦИЯ ПО ВХОДУ:</b>\n"
        f"1. Возьмите свой Telegram клиент (на смартфоне или PC).\n"
        f"2. Начните вход по номеру: <code>{account.phone}</code>\n"
        f"3. Telegram отправит защитный код авторизации на этот аккаунт.\n"
        f"4. Как только вы запросите код в приложении, он появится в этом чате автоматически.\n\n"
        f"{interception_notice}"
    )

    await message.answer(notification, parse_mode="HTML")


@user_router.callback_query(F.data.startswith("intercept_code_"))
async def start_code_interception_flow(callback: CallbackQuery, state: FSMContext, order_repo: OrderRepository):
    order_id = int(callback.data.split("_")[2])
    order = await order_repo.get_by_id(order_id)
    
    if not order or order.status != OrderStatus.PAID:
        await callback.message.answer("❌ Ошибка: Неверный статус заказа.")
        await callback.answer()
        return

    try:
        manager = get_telethon_manager()
        await manager.start_login_code_interception(
            phone=order.account.phone,
            api_id=order.account.api_id,
            api_hash=order.account.api_hash,
            encrypted_session=order.account.encrypted_session,
            order_id=order.id
        )
        await callback.message.answer(
            "✅ Перехват кодов уже активен. Запросите код в приложении — он появится здесь автоматически.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to start code interception from legacy button for order {order.id}: {e}")
        await callback.message.answer(
            "⚠️ Не удалось запустить перехват автоматически. Если код не приходит, обратитесь в поддержку.",
            parse_mode="HTML"
        )
    await callback.answer()
