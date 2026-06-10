import datetime
from typing import List, Optional, Tuple
from sqlalchemy import select, update, and_, or_, desc, asc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from marketplace_bot.models import (
    User, Region, Account, Order, Payment, AdminLog, PaymentLog,
    AccountStatus, OrderStatus, PaymentMethod
)


class BaseRepository:
    def __init__(self, session: AsyncSession):
        self.session = session


class UserRepository(BaseRepository):
    async def get_or_create(self, tg_id: int, username: Optional[str] = None, first_name: str = "") -> User:
        result = await self.session.execute(select(User).where(User.id == tg_id))
        user = result.scalar_one_or_none()
        
        if not user:
            user = User(id=tg_id, username=username, first_name=first_name)
            self.session.add(user)
            await self.session.flush()
        else:
            if user.username != username or user.first_name != first_name:
                user.username = username
                user.first_name = first_name
                await self.session.flush()
        
        return user

    async def get_stats(self) -> dict:
        user_count_res = await self.session.execute(select(User.id))
        user_count = len(user_count_res.all())
        return {"total_users": user_count}


class RegionRepository(BaseRepository):
    async def get_all(self) -> List[Region]:
        result = await self.session.execute(select(Region).order_by(Region.name.asc()))
        return list(result.scalars().all())

    async def get_by_code(self, country_code: str) -> Optional[Region]:
        result = await self.session.execute(select(Region).where(Region.country_code == country_code))
        return result.scalar_one_or_none()

    async def create_region(self, name: str, country_code: str, flag: str) -> Region:
        region = Region(name=name, country_code=country_code, flag=flag)
        self.session.add(region)
        await self.session.flush()
        return region


class AccountRepository(BaseRepository):
    async def add_account(self, phone: str, api_id: int, api_hash: str, encrypted_session: str, price: float, price_stars: int, region_id: int) -> Account:
        # Check if already exists
        existing_res = await self.session.execute(select(Account).where(Account.phone == phone))
        existing = existing_res.scalar_one_or_none()
        if existing:
            from sqlalchemy import delete
            order_ids_res = await self.session.execute(select(Order.id).where(Order.account_id == existing.id))
            order_ids = [r[0] for r in order_ids_res.all()]
            if order_ids:
                await self.session.execute(delete(Payment).where(Payment.order_id.in_(order_ids)))
                await self.session.execute(delete(Order).where(Order.id.in_(order_ids)))
            await self.session.delete(existing)
            await self.session.flush()

        account = Account(
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
            encrypted_session=encrypted_session,
            price=price,
            price_stars=price_stars,
            region_id=region_id,
            status=AccountStatus.PENDING_CLEANUP
        )
        self.session.add(account)
        await self.session.flush()
        return account

    async def get_by_id(self, account_id: int) -> Optional[Account]:
        result = await self.session.execute(
            select(Account).where(Account.id == account_id).options(selectinload(Account.region))
        )
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> Optional[Account]:
        result = await self.session.execute(select(Account).where(Account.phone == phone))
        return result.scalar_one_or_none()

    async def list_available_for_purchase(
        self, region_id: Optional[int] = None, sort_price_desc: Optional[bool] = None
    ) -> List[Account]:
        stmt = select(Account).where(Account.status == AccountStatus.AVAILABLE).options(selectinload(Account.region))
        
        if region_id is not None:
            stmt = stmt.where(Account.region_id == region_id)
            
        if sort_price_desc is not None:
            if sort_price_desc:
                stmt = stmt.order_by(desc(Account.price))
            else:
                stmt = stmt.order_by(asc(Account.price))
        else:
            stmt = stmt.order_by(asc(Account.price))
            
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_pending_order_by_account(self, account_id: int) -> Optional[Order]:
        result = await self.session.execute(
            select(Order)
            .where(and_(Order.account_id == account_id, Order.status == OrderStatus.PENDING))
            .order_by(Order.created_at.desc())
            .options(selectinload(Order.account))
        )
        return result.scalars().first()

    async def fetch_accounts_for_cleanup(self) -> List[Account]:
        # Return accounts that need a safety audit/cleanup
        result = await self.session.execute(
            select(Account).where(Account.status == AccountStatus.PENDING_CLEANUP)
        )
        return list(result.scalars().all())


class OrderRepository(BaseRepository):
    async def create_invoice_with_lock(
        self,
        user_id: int,
        account_id: int,
        payment_method: PaymentMethod,
        ttl_seconds: int,
        ton_rate_usdt: Optional[float] = None,
    ) -> Order:
        """
        Creates a pending order without reserving the account.
        The first successful payment wins the account.
        """
        account_result = await self.session.execute(select(Account).where(Account.id == account_id))
        account = account_result.scalar_one_or_none()

        if not account:
            raise ValueError("Account not found")

        if account.status != AccountStatus.AVAILABLE:
            raise ValueError("This account is not available for purchase")

        # Create the unpaid invoice/order
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=ttl_seconds)
        
        ton_expected_amount = None
        if payment_method == PaymentMethod.TON_TRANSFER:
            if not ton_rate_usdt or ton_rate_usdt <= 0:
                raise ValueError("TON rate is required for TON transfer orders")
            # Freeze the TON amount at invoice creation time so later rate changes do not affect matching.
            ton_expected_amount = round(account.price / ton_rate_usdt, 3)

        order = Order(
            user_id=user_id,
            account_id=account_id,
            price=account.price,
            price_stars=account.price_stars,
            status=OrderStatus.PENDING,
            payment_method=payment_method,
            ton_expected_amount=ton_expected_amount,
            ton_rate_usdt=ton_rate_usdt if payment_method == PaymentMethod.TON_TRANSFER else None,
            expires_at=expires_at
        )
        order.account = account
        self.session.add(order)
        await self.session.flush()

        if payment_method == PaymentMethod.TON_TRANSFER:
            order.ton_comment = f"MKP_{order.id}_{int(datetime.datetime.utcnow().timestamp())}"
            await self.session.flush()
        
        return order

    async def get_by_id(self, order_id: int) -> Optional[Order]:
        result = await self.session.execute(
            select(Order)
            .where(Order.id == order_id)
            .options(selectinload(Order.account), selectinload(Order.user))
        )
        return result.scalar_one_or_none()

    async def get_by_ton_comment(self, comment: str) -> Optional[Order]:
        result = await self.session.execute(
            select(Order)
            .where(and_(Order.ton_comment == comment, Order.status == OrderStatus.PENDING))
            .options(selectinload(Order.account), selectinload(Order.user))
        )
        return result.scalar_one_or_none()

    async def get_by_invoice_id(self, invoice_id: str) -> Optional[Order]:
        result = await self.session.execute(
            select(Order)
            .where(Order.payment_invoice_id == invoice_id)
            .options(selectinload(Order.account), selectinload(Order.user))
        )
        return result.scalar_one_or_none()

    async def apply_successful_payment(
        self,
        order_id: int,
        tx_hash: Optional[str] = None,
        asset: str = "USD",
        amount: Optional[float] = None,
    ) -> Tuple[Order, Account]:
        # Lock order and account for winner-takes-all flow
        order_res = await self.session.execute(
            select(Order).where(Order.id == order_id).options(selectinload(Order.account), selectinload(Order.user)).with_for_update()
        )
        order = order_res.scalar_one()

        if order.status != OrderStatus.PENDING:
            return order, order.account

        account_res = await self.session.execute(
            select(Account).where(Account.id == order.account_id).with_for_update()
        )
        account = account_res.scalar_one()
        if account.status != AccountStatus.AVAILABLE:
            order.status = OrderStatus.CANCELLED
            await self.session.flush()
            return order, account

        order.status = OrderStatus.PAID
        order.paid_at = datetime.datetime.utcnow()
        
        # Record payment item
        payment = Payment(
            order_id=order.id,
            amount=amount if amount is not None else order.price,
            asset=asset,
            tx_hash=tx_hash
        )
        self.session.add(payment)
        
        # Update user totals
        order.user.total_spent += order.price
        
        # Hold account for the paid user until delivery finishes
        account.status = AccountStatus.SOLD
        order.account = account
        await self.session.flush()
        return order, account

    async def cancel_order(self, order_id: int) -> bool:
        """
        Manually cancels an active pending order.
        """
        order_res = await self.session.execute(
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
        )
        order = order_res.scalar_one_or_none()
        if not order or order.status != OrderStatus.PENDING:
            return False

        order.status = OrderStatus.CANCELLED

        await self.session.flush()
        return True

    async def cancel_expired_orders(self) -> List[int]:
        """
        Job method: Finds all pending orders where expires_at < current time and cancels orders.
        """
        now = datetime.datetime.utcnow()
        expired_orders_stmt = select(Order).where(
            and_(Order.status == OrderStatus.PENDING, Order.expires_at < now)
        )
        
        result = await self.session.execute(expired_orders_stmt)
        expired_orders = result.scalars().all()
        
        cancelled_ids = []
        for order in expired_orders:
            order.status = OrderStatus.CANCELLED
            cancelled_ids.append(order.id)
            
        if expired_orders:
            await self.session.flush()
            
        return cancelled_ids


class AdminLogRepository(BaseRepository):
    async def log_action(self, admin_id: int, action: str, details: str, admin_username: Optional[str] = None, admin_first_name: Optional[str] = None) -> AdminLog:
        # Prevent ForeignKeyViolationError on admin_logs_admin_id_fkey
        user_res = await self.session.execute(select(User).where(User.id == admin_id))
        user = user_res.scalar_one_or_none()
        if not user:
            user = User(
                id=admin_id,
                username=admin_username or "admin",
                first_name=admin_first_name or "Administrator"
            )
            self.session.add(user)
            await self.session.flush()
        elif admin_username or admin_first_name:
            if admin_username and user.username != admin_username:
                user.username = admin_username
            if admin_first_name and user.first_name != admin_first_name:
                user.first_name = admin_first_name
            await self.session.flush()

        log = AdminLog(admin_id=admin_id, action=action, details=details)
        self.session.add(log)
        await self.session.flush()
        return log


class PaymentLogRepository(BaseRepository):
    async def log_webhook(self, gateway: str, payload_str: str) -> PaymentLog:
        log = PaymentLog(gateway=gateway, payload=payload_str)
        self.session.add(log)
        await self.session.flush()
        return log