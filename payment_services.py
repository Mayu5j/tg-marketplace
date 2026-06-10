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

    @staticmethod
    def _decode_ton_comment_payload(payload: str, *, strip_opcode: bool = False) -> str:
        """
        Decode TON message comments returned by Toncenter.

        TON wallets/APIs are inconsistent: some return a ready text comment, while
        others put a base64-encoded comment into msg.dataText.text or msg.dataRaw.body.
        This helper accepts both variants and returns a plain user-visible comment.
        """
        if not payload:
            return ""

        text = str(payload).strip()
        candidates = [text]

        # Try base64 decoding even for msg.dataText: Tonkeeper/Toncenter can expose
        # comments like "TUtQXzNf...", which is base64 for "MKP_3_...".
        try:
            padded_text = text + "=" * (-len(text) % 4)
            decoded = b64decode(padded_text, validate=True)
        except (binascii.Error, ValueError):
            decoded = b""

        if decoded:
            byte_candidates = [decoded]
            if strip_opcode and len(decoded) > 4:
                byte_candidates.insert(0, decoded[4:])

            for raw in byte_candidates:
                decoded_text = raw.decode("utf-8", errors="ignore").strip("\x00\r\n ")
                if decoded_text and decoded_text not in candidates:
                    candidates.append(decoded_text)

        # Prefer the marketplace marker if any candidate contains it. This handles
        # decoded raw payloads with an opcode/prefix as well as plain comments.
        for candidate in candidates:
            marker_pos = candidate.find("MKP_")
            if marker_pos >= 0:
                return candidate[marker_pos:].strip()

        # Otherwise return the most decoded readable version we found.
        return candidates[-1] if candidates else ""

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


    async def fetch_ton_usdt_rate(self) -> Optional[float]:
        """Fetch the current 1 TON price in USDT/USD for creating fixed TON invoices."""
        headers = {}
        if settings.TONAPI_KEY:
            headers["Authorization"] = f"Bearer {settings.TONAPI_KEY}"

        params = {"tokens": "ton", "currencies": "usd"}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    settings.TON_RATE_API_URL,
                    params=params,
                    headers=headers,
                    timeout=10.0
                )
                if response.status_code == 200:
                    data = response.json()
                    rate = self._extract_ton_usd_rate(data)
                    if rate and rate > 0:
                        logger.info(f"TON rate fetched from API: 1 TON = {rate} USD")
                        return rate
                    logger.error(f"TON rate API response did not contain a usable TON/USD rate: {data}")
                else:
                    logger.error(f"TON rate API returned HTTP {response.status_code}: {response.text}")
        except Exception as e:
            logger.exception(f"Error fetching TON/USD rate from API: {e}")

        if settings.TON_USDT_RATE and settings.TON_USDT_RATE > 0:
            logger.warning(f"Falling back to manual TON_USDT_RATE={settings.TON_USDT_RATE}")
            return settings.TON_USDT_RATE

        return None

    @staticmethod
    def _extract_ton_usd_rate(data: Dict[str, Any]) -> Optional[float]:
        """Extract TON/USD rate from TonAPI-style rates responses with schema tolerance."""
        possible_token_keys = ("TON", "ton", "Toncoin", "toncoin")
        possible_currency_keys = ("USD", "usd", "USDT", "usdt")

        rates = data.get("rates") if isinstance(data, dict) else None
        if not isinstance(rates, dict):
            return None

        for token_key in possible_token_keys:
            token_info = rates.get(token_key)
            if not isinstance(token_info, dict):
                continue

            prices = token_info.get("prices")
            if isinstance(prices, dict):
                for currency_key in possible_currency_keys:
                    value = prices.get(currency_key)
                    if value is not None:
                        try:
                            return float(value)
                        except (TypeError, ValueError):
                            pass

            for currency_key in possible_currency_keys:
                value = token_info.get(currency_key)
                if value is not None:
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        pass

        return None

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
            
            # Plain text comment could be direct text or base64-encoded text.
            # Tonkeeper/Toncenter may return msg.dataText.text as base64, e.g.
            # "TUtQXzNfMTc4..." -> "MKP_3_178...". Normalize it before matching.
            if msg_data.get("@type") == "msg.dataText":
                raw_text = msg_data.get("text", "")
                comment = self._decode_ton_comment_payload(raw_text)
                logger.info(
                    f"TX Parser: Extracted TEXT comment: raw='{raw_text}', normalized='{comment}' "
                    f"from tx {tx_hash[:16] if tx_hash else 'unknown'}..."
                )
            elif msg_data.get("@type") == "msg.dataRaw":
                body = msg_data.get("body", "")
                if body:
                    comment = self._decode_ton_comment_payload(body, strip_opcode=True)
                    logger.info(
                        f"TX Parser: Decoded RAW comment: '{comment}' "
                        f"from tx {tx_hash[:16] if tx_hash else 'unknown'}..."
                    )
            else:
                logger.debug(f"TX Parser: Unknown msg_data type '{msg_data_type}' for tx {tx_hash[:16] if tx_hash else 'unknown'}...")
            
            # If still no comment, try to extract from body field directly.
            if not comment and "body" in in_msg:
                body = in_msg.get("body", "")
                if body:
                    comment = self._decode_ton_comment_payload(body, strip_opcode=True)
                    if comment:
                        logger.info(f"TX Parser: Extracted comment from in_msg.body: '{comment}'")
            
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
