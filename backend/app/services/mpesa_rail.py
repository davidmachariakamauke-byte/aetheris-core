import base64
from datetime import datetime
import os
import requests
from zoneinfo import ZoneInfo

class MpesaRail:
    def __init__(self):
        self.consumer_key = os.getenv("DARAJA_CONSUMER_KEY")
        self.consumer_secret = os.getenv("DARAJA_CONSUMER_SECRET")
        self.shortcode = os.getenv("DARAJA_SHORTCODE", "174379")
        self.passkey = os.getenv("DARAJA_PASSKEY")
        self.base_url = os.getenv("DARAJA_BASE_URL", "https://sandbox.safaricom.co.ke")

    def get_access_token(self) -> str:
        credentials = f"{self.consumer_key}:{self.consumer_secret}"
        encoded_creds = base64.b64encode(credentials.encode()).decode()
        headers = {"Authorization": f"Basic {encoded_creds}"}
        url = f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials"

        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()["access_token"]

    def trigger_stk_push(self, phone_number: str, amount: int, account_ref: str, callback_url: str):
        access_token = self.get_access_token()
        timestamp = datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y%m%d%H%M%S")
        password_str = f"{self.shortcode}{self.passkey}{timestamp}"
        password = base64.b64encode(password_str.encode()).decode()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": amount,
            "PartyA": phone_number,
            "PartyB": self.shortcode,
            "PhoneNumber": phone_number,
            "CallBackURL": callback_url,
            "AccountReference": account_ref,
            "TransactionDesc": "Aetheris Match Stake",
        }

        response = requests.post(
            f"{self.base_url}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers=headers,
        )
        return response.json()
