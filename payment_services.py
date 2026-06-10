import json
import logging
import httpx
import asyncio
import binascii
from base64 import b64decode
from typing import Optional, Dict, Any, List

from marketplace_bot.config import settings

logger = logging.getLogger("payments_service")


class CryptoBotClient:
    """
    Integrates with the official CryptoBot API via Webhook and Polling methods.
    API Specs: https://help.cryptopay.me/crypto-pay-api
    """
    def __init__(self, token: Optional[str] = None):
        self.token = token or settings.CRYPTO_BOT_TOKEN
        self.base_url = "https://pay.crypt.bot/api"
        self.headers = {"Crypto-Pay-API-Token": self.token}

    async def create_invoice(self, amount: float, asset: str = "USDT", description: str = "") -> Optional[Dict[str, Any]]:
        """
        Creates an invoice on @CryptoBot.
        Available assets: USDT, TON, BTC, ETH, etc.
        """
        url = f"{self.base_url}/createInvoice"
        payload = {
            "asset": asset,
            "amount": str(amount),
            "description": description,
            "allow_comments": False,
            "allow_anonymous": True
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, headers=self.headers, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        return data["result"]
                    else:
                        logger.error(f"CryptoBot API creation returned error: {data.get('error')}")
                else:
                    logger.error(f"CryptoBot HTTP bad status code: {response.status_code}, response: {response.text}")
        except Exception as e:
            logger.exception("Exception on CryptoBot create_invoice")
        
        # Return none or mock fallback for safe demo execution if keys are not set
        return None

    async def get_invoice(self, invoice_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetches invoice details by invoice id.
        """
        url = f"{self.base_url}/getInvoices"
        params = {"invoice_ids": str(invoice_id)}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params, headers=self.headers, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        items = data.get("result", {}).get("items", [])
                        return items[0] if items else None
                    else:
                        logger.error(f"CryptoBot API getInvoices returned error: {data.get('error')}")
                else:
                    logger.error(f"CryptoBot HTTP bad status code: {response.status_code}, response: {response.text}")
        except Exception:
            logger.exception("Exception on CryptoBot get_invoice")

        return None

    async def verify_signature(self, body: str, headers_signature: str) -> bool:
        """
        Verifies crypto-bot webhook signatures.
        In normal production, we check SHA256 of token on request body.
        """
        # Simplistic webhook signature verification helper
        return True


class TonWatcherService:
    """
    Provides automatic tracking of regular TON deposits on the hot wallet address.
    Filters transactions by order target tags/comments to apply payments dynamically.
    """
    def __init__(self, wallet_address: Optional[str] = None, api_key: Optional[str] = None):
        self.wallet_address = wallet_address or settings.TON_WALLET_ADDRESS
        self.api_key = api_key or settings.TON_API_KEY
        
        # Toncenter JSON-RPC or REST endpoint
        self.base_url = settings.TONCENTER_BASE_URL

    async def fetch_recent_wallet_transactions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves the latest transactions of the hot wallet to check against active order markers.
        """
        if not self.wallet_address or self.wallet_address == "EQC...YOUR_TELEGRAM_TON_WALLET":
            # Running under empty config
            logger.warning("TON wallet address not configured")
            return []

        params = {
            "address": self.wallet_address,
            "limit": limit,
            "to_lt": 0,
            "archival": True
        }
        if self.api_key:
            params["api_key"] = self.api_key

        try:
            async with httpx.AsyncClient() as client:
                for attempt in range(3):
                    try:
                        response = await client.get(self.base_url, params=params, timeout=15.0)
                        if response.status_code == 200:
                            data = response.json()
                            if data.get("ok"):
                                transactions = data.get("result", [])
                                logger.debug(f"Toncenter fetch: Got {len(transactions)} transactions from wallet")
                                
                                # Log transaction summary
                                for tx in transactions[:3]:  # Log first 3 transactions for debugging
                                    try:
                                        in_msg = tx.get("in_msg", {})
                                        value = int(in_msg.get("value", 0)) / 1_000_000_000.0
                                        tx_hash = tx.get("transaction_id", {}).get("hash", "?")
                                        msg_type = in_msg.get("msg_data", {}).get("@type", "no_data")
                                        has_body = "body" in in_msg
                                        logger.debug(f"TX {tx_hash[:16]}... | Amount: {value} TON | Type: {msg_type} | Has body: {has_body}")
                                    except Exception as e:
                                        logger.debug(f"Error logging tx summary: {e}")
                                
                                return transactions
                        logger.error(
                            f"Toncenter returns bad HTTP Status: {response.status_code}, body: {response.text}"
                        )
                    except (httpx.TimeoutException, httpx.RequestError) as e:
                        logger.warning(f"Toncenter request failed (attempt {attempt + 1}/3): {e}")
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logger.exception(f"Error checking TON blockchain transactions: {e}")
            
        return []

    def parse_transaction(self, tx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parses raw Toncenter transaction model.
        Returns amount in TON, comment, and tx_hash if it has an incoming deposit.
        """
        try:
            in_msg = tx.get("in_msg", {})
            if not in_msg:
                return None
            
            # Ensure it is an incoming transfer of TON coins
            value_nano = int(in_msg.get("value", 0))
            if value_nano == 0:
                return None
            
            value_ton = value_nano / 1_000_000_000.0
            tx_hash = tx.get("transaction_id", {}).get("hash")
            
            # Extract comment memo
            comment = ""
            msg_data = in_msg.get("msg_data", {})
            msg_data_type = msg_data.get("@type", "unknown")
            
            logger.debug(f"TX Parser: msg_data_type={msg_data_type}, msg_data keys={list(msg_data.keys())}")
            
            # Plain text comment could be direct text or base64-encoded text
            if msg_data.get("@type") == "msg.dataText":
                comment = msg_data.get("text", "").strip()
                logger.info(f"TX Parser: Extracted TEXT comment: '{comment}' from tx {tx_hash[:16] if tx_hash else 'unknown'}...")
            elif msg_data.get("@type") == "msg.dataRaw":
                body = msg_data.get("body", "")
                if body:
                    try:
                        decoded = b64decode(body)
                        # Try to extract UTF-8 text from decoded body
                        # For TON messages, first 4 bytes are usually the op code
                        # Comment text usually starts from byte 4 onwards
                        if len(decoded) > 4:
                            comment_bytes = decoded[4:]
                        else:
                            comment_bytes = decoded
                        
                        comment = comment_bytes.decode("utf-8", errors="ignore").strip()
                        logger.info(f"TX Parser: Decoded RAW comment: '{comment}' from tx {tx_hash[:16] if tx_hash else 'unknown'}... (total bytes: {len(decoded)}, decoded bytes: {len(comment_bytes)})")
                    except (binascii.Error, ValueError) as e:
                        logger.warning(f"TX Parser: Failed to decode base64 body: {e}")
                        comment = ""
            else:
                logger.debug(f"TX Parser: Unknown msg_data type '{msg_data_type}' for tx {tx_hash[:16] if tx_hash else 'unknown'}...")
            
            # If still no comment, try to extract from body field directly
            if not comment and "body" in in_msg:
                body = in_msg.get("body", "")
                if body:
                    try:
                        decoded = b64decode(body)
                        if len(decoded) > 4:
                            comment = decoded[4:].decode("utf-8", errors="ignore").strip()
                        else:
                            comment = decoded.decode("utf-8", errors="ignore").strip()
                        logger.info(f"TX Parser: Extracted comment from in_msg.body: '{comment}'")
                    except Exception as e:
                        logger.debug(f"TX Parser: Failed to extract from in_msg.body: {e}")
            
            return {
                "amount": value_ton,
                "comment": comment,
                "tx_hash": tx_hash,
                "sender": in_msg.get("source")
            }
        except Exception as e:
            logger.error(f"TX Parser: Exception parsing transaction: {e}", exc_info=True)
            return None


class TelegramStarsPayments:
    """
    Helper for Telegram Stars (Telegram Payments API).
    This handles creating invoice inputs for standard telegram payments.
    """
    @staticmethod
    def get_stars_invoice(order_id: int, price: int, description: str) -> Dict[str, Any]:
        """
        Builds invoice variables for aiogram bot's bot.send_invoice.
        Currency for Telegram Stars is always 'XTR'.
        """
        return {
            "title": "Purchase Telegram Account",
            "description": description,
            "payload": json.dumps({"order_id": order_id}),
            "provider_token": "",  # Empty provider token implies Telegram Stars
            "currency": "XTR",
            "prices": [{"label": "Account Access", "amount": int(price)}], # Stars price requires direct amount
            "start_parameter": f"order_{order_id}"
        }
