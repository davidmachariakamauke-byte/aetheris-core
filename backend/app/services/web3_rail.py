import logging
import os
import asyncio
from typing import Optional
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

logger = logging.getLogger("AETHERIS-WEB3")

class Web3EscrowService:
    """Interacts with the deployed AetherisEscrow smart contract to trigger automatic USDT payouts."""

    def __init__(self):
        self.rpc_url = os.getenv("WEB3_RPC_URL", "https://polygon-rpc.com")
        self.private_key = os.getenv("ORACLE_PRIVATE_KEY", "")
        self.contract_address = os.getenv("ESCROW_CONTRACT_ADDRESS", "0x0000000000000000000000000000000000000000")

        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        if self.private_key:
            self.account = self.w3.eth.account.from_key(self.private_key)
        else:
            self.account = None

        self.abi = [
            {
                "inputs": [
                    {"internalType": "bytes32", "name": "matchId", "type": "bytes32"},
                    {"internalType": "address", "name": "winner", "type": "address"}
                ],
                "name": "releaseWinnerPayout",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            }
        ]

    async def release_escrow(self, match_id: str, winner_address: str, amount: float) -> str:
        if not self.account or self.contract_address == "0x0000000000000000000000000000000000000000":
            logger.warning("Web3 not fully configured. Executing simulated blockchain settlement.")
            await asyncio.sleep(0.3)
            return f"0xSIMULATED_{match_id[:8]}"

        contract = self.w3.eth.contract(address=self.w3.to_checksum_address(self.contract_address), abi=self.abi)
        match_id_bytes = self.w3.keccak(text=match_id)
        winner_checksum = self.w3.to_checksum_address(winner_address)

        nonce = self.w3.eth.get_transaction_count(self.account.address)
        tx = contract.functions.releaseWinnerPayout(match_id_bytes, winner_checksum).build_transaction({
            'from': self.account.address,
            'nonce': nonce,
            'gas': 200000,
            'maxFeePerGas': self.w3.to_wei('50', 'gwei'),
            'maxPriorityFeePerGas': self.w3.to_wei('2', 'gwei')
        })

        signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        return self.w3.to_hex(tx_hash)
