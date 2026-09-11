import base64
import logging
import os
import time
from datetime import datetime
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger("AETHERIS-MPESA")

class MPesaRailService:
    """Production-grade Safaricom Daraja API service handling OAuth token caching, STK Push, and B2C payouts."""

    def __init__(self):
        self.consumer_key = os.getenv("MPESA_CONSUMER_KEY", "")
        self.consumer_secret = os.getenv("MPESA_CONSUMER_SECRET", "")
        self.shortcode = os.getenv("MPESA_SHORTCODE", "174379")
        self.passkey = os.getenv("MPESA_PASSKEY", "")
        self.initiator_name = os.getenv("MPESA_INITIATOR_NAME", "aetheris_admin")
        self.security_credential = os.getenv("MPESA_SECURITY_CREDENTIAL", "")
        self.env = os.getenv("MPESA_ENV", "sandbox").lower()

        self.base_url = "https://api.safaricom.co.ke" if self.env == "production" else "https://sandbox.safaricom.co.ke"
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def _format_phone_number(self, phone: str) -> str:
        cleaned = "".join(filter(str.isdigit, phone))
        if cleaned.startswith("0") and len(cleaned) == 10:
            return f"254{cleaned[1:]}"
        elif (cleaned.startswith("7") or cleaned.startswith("1")) and len(cleaned) == 9:
            return f"254{cleaned}"
        elif cleaned.startswith("254") and len(cleaned) == 12:
            return cleaned
        raise ValueError(f"Invalid Kenyan phone number format: {phone}")

    async def _get_access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        url = f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url, auth=(self.consumer_key, self.consumer_secret))
                response.raise_for_status()
                data = response.json()
                self._token = data.get("access_token")
                expires_in = int(data.get("expires_in", 3599))
                self._token_expires_at = time.time() + expires_in
                return self._token
            except Exception as e:
                logger.error(f"M-Pesa Token Error: {str(e)}")
                raise RuntimeError(f"M-Pesa Auth Error: {str(e)}")

    def _generate_password(self, timestamp: str) -> str:
        raw_str = f"{self.shortcode}{self.passkey}{timestamp}"
        return base64.b64encode(raw_str.encode("utf-8")).decode("utf-8")

    async def initiate_stk_push(self, phone_number: str, amount: float, match_id: str, callback_url: str) -> Dict[str, Any]:
        formatted_phone = self._format_phone_number(phone_number)
        token = await self._get_access_token()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        password = self._generate_password(timestamp)

        url = f"{self.base_url}/mpesa/stkpush/v1/processrequest"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(amount),
            "PartyA": formatted_phone,
            "PartyB": self.shortcode,
            "PhoneNumber": formatted_phone,
            "CallBackURL": callback_url,
            "AccountReference": match_id[:12],
            "TransactionDesc": f"AETHERIS Stake {match_id}"
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    async def execute_b2c_payout(self, phone_number: str, amount: float, match_id: str, result_url: str, timeout_url: str) -> Dict[str, Any]:
        formatted_phone = self._format_phone_number(phone_number)
        token = await self._get_access_token()

        url = f"{self.base_url}/mpesa/b2c/v1/paymentrequest"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "InitiatorName": self.initiator_name,
            "SecurityCredential": self.security_credential,
            "CommandID": "BusinessPayment",
            "Amount": int(amount),
            "PartyA": self.shortcode,
            "PartyB": formatted_phone,
            "Remarks": f"AETHERIS Victory {match_id}",
            "QueueTimeOutURL": timeout_url,
            "ResultURL": result_url,
            "Occasion": "Victory Payout"
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
