import asyncio
import json
import time
from decimal import Decimal
from pprint import pprint
from typing import Optional, Tuple, Union, Dict, Any

from tronpy import keys
from tronpy.abi import tron_abi
from tronpy.async_contract import AsyncContract, AsyncContractMethod, ShieldedTRC20
from tronpy.defaults import conf_for_name
from tronpy.exceptions import (
    AddressNotFound,
    ApiError,
    AssetNotFound,
    BadHash,
    BadKey,
    BadSignature,
    BlockNotFound,
    BugInJavaTron,
    TaposError,
    TransactionError,
    TransactionNotFound,
    TvmError,
    UnknownError,
    ValidationError,
)
from tronpy.hdwallet import TRON_DEFAULT_PATH, generate_mnemonic, key_from_seed, seed_from_mnemonic
from tronpy.keys import PrivateKey
from tronpy.providers.async_http import AsyncHTTPProvider

TAddress = str

DEFAULT_CONF = {
    "fee_limit": 10_000_000,
    "timeout": 10.0,  # in second
}


def current_timestamp() -> int:
    return int(time.time() * 1000)


# noinspection PyBroadException
class AsyncTransactionRet(dict):
    def __init__(self, iterable, client: "AsyncTron", method: AsyncContractMethod = None):
        super().__init__(iterable)

        self._client = client
        self._txid = self["txid"]
        self._method = method

    @property
    def txid(self):
        """The transaction id in hex."""
        return self._txid

    async def wait(self, timeout=30, interval=1.6, solid=False) -> dict:
        """Wait the transaction to be on chain.

        :returns: TransactionInfo
        """

        get_transaction_info = self._client.get_transaction_info
        if solid:
            get_transaction_info = self._client.get_solid_transaction_info

        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                return await get_transaction_info(self._txid)
            except TransactionNotFound:
                await asyncio.sleep(interval)

        raise TransactionNotFound("timeout and can not find the transaction")

    async def result(self, timeout=30, interval=1.6, solid=False) -> dict:
        """Wait the contract calling result.

        :returns: Result of contract method
        """
        if self._method is None:
            raise TypeError("Not a smart contract call")

        receipt = await self.wait(timeout, interval, solid)

        if receipt.get("result", None) == "FAILED":
            msg = receipt.get("resMessage", receipt["result"])

            if receipt["receipt"]["result"] == "REVERT":
                try:
                    result = receipt.get("contractResult", [])
                    if result and len(result[0]) > (4 + 32) * 2:
                        error_msg = tron_abi.decode_single("string", bytes.fromhex(result[0])[4 + 32 :])
                        msg = f"{msg}: {error_msg}"
                except Exception:
                    pass
            raise TvmError(msg)

        return self._method.parse_output(receipt["contractResult"][0])


EMPTY = object()


# noinspection PyBroadException,PyProtectedMember
class AsyncTransaction:
    """The Transaction object, signed or unsigned."""

    def __init__(
        self,
        raw_data: dict,
        client: "AsyncTron" = None,
        method: AsyncContractMethod = None,
        txid: str = "",
        permission: dict = None,
        signature: list = None,
    ):
        self._raw_data: dict = raw_data.get("raw_data", raw_data)
        self._signature: list = raw_data.get("signature", signature or [])
        self._client = client

        self._method = method

        self.txid: str = raw_data.get("txID", txid)
        """The transaction id in hex."""

        self._permission: Optional[dict] = raw_data.get("permission", permission)

        # IMPORTANT must use "Transaction.create" to create a new Transaction

    @classmethod
    async def create(cls, *args, **kwargs) -> Optional["AsyncTransaction"]:
        _tx = cls(*args, **kwargs)
        if not _tx.txid or _tx._permission is EMPTY:
            await _tx.check_sign_weight()
        return _tx

    async def check_sign_weight(self):
        sign_weight = await self._client.get_sign_weight(self)
        if "transaction" not in sign_weight:
            self._client._handle_api_error(sign_weight)
            raise TransactionError("transaction not in sign_weight")
        self.txid = sign_weight["transaction"]["transaction"]["txID"]
        # when account not exist on-chain
        self._permission = sign_weight.get("permission", None)

    def to_json(self) -> dict:
        return {
            "txID": self.txid,
            "raw_data": self._raw_data,
            "signature": self._signature,
            "permission": self._permission if self._permission is not EMPTY else None,
        }

    @classmethod
    async def from_json(cls, data: Union[str, dict], client: "AsyncTron" = None) -> "AsyncTransaction":
        if isinstance(json, str):
            data = json.loads(data)
        return await cls.create(
            client=client,
            txid=data["txID"],
            permission=data["permission"],
            raw_data=data["raw_data"],
            signature=data["signature"],
        )

    def inspect(self) -> "AsyncTransaction":
        pprint(self.to_json())
        return self

    def sign(self, priv_key: PrivateKey) -> "AsyncTransaction":
        """Sign the transaction with a private key."""

        assert self.txid, "txID not calculated"
        assert self.is_expired is False, "expired"

        if self._permission is not None:
            addr_of_key = priv_key.public_key.to_hex_address()
            for key in self._permission["keys"]:
                if key["address"] == addr_of_key:
                    break
            else:
                raise BadKey(
                    "provided private key is not in the permission list",
                    f"provided {priv_key.public_key.to_base58check_address()}",
                    f"required {self._permission}",
                )
        sig = priv_key.sign_msg_hash(bytes.fromhex(self.txid))
        self._signature.append(sig.hex())
        return self

    async def broadcast(self) -> AsyncTransactionRet:
        """Broadcast the transaction to TRON network."""
        return AsyncTransactionRet(await self._client.broadcast(self), client=self._client, method=self._method)

    def set_signature(self, signature: list) -> "AsyncTransaction":
        """set async transaction signature"""
        self._signature = signature
        return self

    @property
    def is_expired(self) -> bool:
        return current_timestamp() >= self._raw_data["expiration"]

    async def update(self):
        """update Transaction, change ref_block and txID, remove all signature"""
        self._raw_data["timestamp"] = current_timestamp()
        self._raw_data["expiration"] = self._raw_data["timestamp"] + 60_000
        ref_block_id = await self._client.get_latest_solid_block_id()
        # last 2 byte of block number part
        self._raw_data["ref_block_bytes"] = ref_block_id[12:16]
        # last half part of block hash
        self._raw_data["ref_block_hash"] = ref_block_id[16:32]

        self.txid = ""
        self._permission = None
        self._signature = []
        sign_weight = await self._client.get_sign_weight(self)
        if "transaction" not in sign_weight:
            self._client._handle_api_error(sign_weight)
            return  # unreachable
        self.txid = sign_weight["transaction"]["transaction"]["txID"]

        # when account not exist on-chain
        self._permission = sign_weight.get("permission", None)
        # remove all _signature
        self._signature = []

    def __str__(self):
        return json.dumps(self.to_json(), indent=2)


# noinspection PyBroadException
class AsyncTransactionBuilder:
    """TransactionBuilder, to build a :class:`~Transaction` object."""

    def __init__(self, inner: dict, client: "AsyncTron", method: AsyncContractMethod = None):
        self._client = client
        self._raw_data = {
            "contract": [inner],
            "timestamp": current_timestamp(),
            "expiration": current_timestamp() + 60_000,
            "ref_block_bytes": None,
            "ref_block_hash": None,
        }

        if inner.get("type", None) in ["TriggerSmartContract", "CreateSmartContract"]:
            self._raw_data["fee_limit"] = self._client.conf["fee_limit"]

        self._method = method

    def with_owner(self, addr: TAddress) -> "AsyncTransactionBuilder":
        """Set owner of the transaction."""
        if "owner_address" in self._raw_data["contract"][0]["parameter"]["value"]:
            self._raw_data["contract"][0]["parameter"]["value"]["owner_address"] = keys.to_hex_address(addr)
        else:
            raise TypeError("can not set owner")
        return self

    def permission_id(self, perm_id: int) -> "AsyncTransactionBuilder":
        """Set permission_id of the transaction."""
        self._raw_data["contract"][0]["Permission_id"] = perm_id
        return self

    def memo(self, memo: Union[str, bytes]) -> "AsyncTransactionBuilder":
        """Set memo of the transaction."""
        data = memo.encode() if isinstance(memo, (str,)) else memo
        self._raw_data["data"] = data.hex()
        return self

    def expiration(self, expiration: int) -> "AsyncTransactionBuilder":
        self._raw_data["expiration"] = current_timestamp() + expiration
        return self

    def fee_limit(self, value: int) -> "AsyncTransactionBuilder":
        """Set fee_limit of the transaction, in `SUN`."""
        self._raw_data["fee_limit"] = value
        return self

    async def build(self, options=None, **kwargs) -> AsyncTransaction:
        """Build the transaction."""
        ref_block_id = await self._client.get_latest_solid_block_id()
        # last 2 byte of block number part
        self._raw_data["ref_block_bytes"] = ref_block_id[12:16]
        # last half part of block hash
        self._raw_data["ref_block_hash"] = ref_block_id[16:32]

        if self._method:
            return await AsyncTransaction.create(self._raw_data, client=self._client, method=self._method)

        return await AsyncTransaction.create(self._raw_data, client=self._client)


# noinspection PyBroadException
class AsyncTrx:
    """The Trx(transaction) API."""

    def __init__(self, tron):
        self._tron = tron

    @property
    def client(self) -> "AsyncTron":
        return self._tron

    def _build_transaction(self, type_: str, obj: dict, *, method: AsyncContractMethod = None) -> AsyncTransactionBuilder:
        inner = {
            "parameter": {"value": obj, "type_url": f"type.googleapis.com/protocol.{type_}"},
            "type": type_,
        }
        if method:
            return AsyncTransactionBuilder(inner, client=self.client, method=method)
        return AsyncTransactionBuilder(inner, client=self.client)

    def transfer(self, from_: TAddress, to: TAddress, amount: int) -> AsyncTransactionBuilder:
        """Transfer TRX. ``amount`` in `SUN`."""
        return self._build_transaction(
            "TransferContract",
            {"owner_address": keys.to_hex_address(from_), "to_address": keys.to_hex_address(to), "amount": amount},
        )

    # TRC10 asset

    def asset_transfer(self, from_: TAddress, to: TAddress, amount: int, token_id: int) -> AsyncTransactionBuilder:
        """Transfer TRC10 tokens."""
        return self._build_transaction(
            "TransferAssetContract",
            {
                "owner_address": keys.to_hex_address(from_),
                "to_address": keys.to_hex_address(to),
                "amount": amount,
                "asset_name": str(token_id).encode().hex(),
            },
        )

    def asset_issue(
        self,
        owner: TAddress,
        abbr: str,
        total_supply: int,
        *,
        url: str,
        name: str = None,
        description: str = "",
        start_time: int = None,
        end_time: int = None,
        precision: int = 6,
        frozen_supply: list = None,
        trx_num: int = 1,
        num: int = 1,
    ) -> AsyncTransactionBuilder:
        """Issue a TRC10 token.

        Almost all parameters have resonable defaults.
        """
        if name is None:
            name = abbr

        if start_time is None:
            # use default expiration
            start_time = current_timestamp() + 60_000

        if end_time is None:
            # use default expiration
            end_time = current_timestamp() + 60_000 + 1

        if frozen_supply is None:
            frozen_supply = []

        return self._build_transaction(
            "AssetIssueContract",
            {
                "owner_address": keys.to_hex_address(owner),
                "abbr": abbr.encode().hex(),
                "name": name.encode().hex(),
                "total_supply": total_supply,
                "precision": precision,
                "url": url.encode().hex(),
                "description": description.encode().hex(),
                "start_time": start_time,
                "end_time": end_time,
                "frozen_supply": frozen_supply,
                "trx_num": trx_num,
                "num": num,
                "public_free_asset_net_limit": 0,
                "free_asset_net_limit": 0,
            },
        )

    # Account

    def account_permission_update(self, owner: TAddress, perm: dict) -> "AsyncTransactionBuilder":
        """Update account permission.

        :param owner: Address of owner
        :param perm: Permission dict from :meth:`~tronpy.Tron.get_account_permission`
        """

        if "owner" in perm:
            for key in perm["owner"]["keys"]:
                key["address"] = keys.to_hex_address(key["address"])
        if "actives" in perm:
            for act in perm["actives"]:
                for key in act["keys"]:
                    key["address"] = keys.to_hex_address(key["address"])
        if perm.get("witness", None):
            for key in perm["witness"]["keys"]:
                key["address"] = keys.to_hex_address(key["address"])

        return self._build_transaction(
            "AccountPermissionUpdateContract",
            dict(owner_address=keys.to_hex_address(owner), **perm),
        )

    def account_update(self, owner: TAddress, name: str) -> "AsyncTransactionBuilder":
        """Update account name. An account can only set name once."""
        return self._build_transaction(
            "UpdateAccountContract",
            {"owner_address": keys.to_hex_address(owner), "account_name": name.encode().hex()},
        )

    def freeze_balance(self, owner: TAddress, amount: int, resource: str = "ENERGY") -> "AsyncTransactionBuilder":
        """Freeze balance to get energy or bandwidth, for 3 days.

        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        """
        payload = {
            "owner_address": keys.to_hex_address(owner),
            "frozen_balance": amount,
            "resource": resource,
        }
        return self._build_transaction("FreezeBalanceV2Contract", payload)

    def withdraw_stake_balance(self, owner: TAddress) -> "AsyncTransactionBuilder":
        """Withdraw all stake v2 balance after waiting for 14 days since unfreeze_balance call.

        :param owner:
        """
        payload = {
            "owner_address": keys.to_hex_address(owner),
        }
        return self._build_transaction("WithdrawExpireUnfreezeContract", payload)

    def unfreeze_balance(
        self, owner: TAddress, resource: str = "ENERGY", *, unfreeze_balance: int
    ) -> "AsyncTransactionBuilder":
        """Unfreeze balance to get TRX back.

        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        """
        payload = {
            "owner_address": keys.to_hex_address(owner),
            "unfreeze_balance": unfreeze_balance,
            "resource": resource,
        }
        return self._build_transaction("UnfreezeBalanceV2Contract", payload)

    def unfreeze_balance_legacy(
        self, owner: TAddress, resource: str = "ENERGY", receiver: TAddress = None
    ) -> "AsyncTransactionBuilder":
        """Unfreeze balance to get TRX back.

        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        """
        payload = {
            "owner_address": keys.to_hex_address(owner),
            "resource": resource,
        }
        if receiver is not None:
            payload["receiver_address"] = keys.to_hex_address(receiver)
        return self._build_transaction("UnfreezeBalanceContract", payload)

    def delegate_resource(
        self,
        owner: TAddress,
        receiver: TAddress,
        balance: int,
        resource: str = "BANDWIDTH",
        lock: bool = False,
        lock_period: int = None,
    ) -> "AsyncTransactionBuilder":
        """Delegate bandwidth or energy resources to other accounts in Stake2.0.

        :param owner:
        :param receiver:
        :param balance:
        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        :param lock: Optionally lock delegated resources for 3 days.
        :param lock_period: Optionally lock delegated resources for a specific period. Default: 3 days.
        """

        payload = {
            "owner_address": keys.to_hex_address(owner),
            "receiver_address": keys.to_hex_address(receiver),
            "balance": balance,
            "resource": resource,
            "lock": lock,
        }
        if lock_period is not None:
            payload["lock_period"] = lock_period

        return self._build_transaction("DelegateResourceContract", payload)

    def undelegate_resource(
        self, owner: TAddress, receiver: TAddress, balance: int, resource: str = "BANDWIDTH"
    ) -> "AsyncTransactionBuilder":
        """Cancel the delegation of bandwidth or energy resources to other accounts in Stake2.0

        :param owner:
        :param receiver:
        :param balance:
        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        """

        payload = {
            "owner_address": keys.to_hex_address(owner),
            "receiver_address": keys.to_hex_address(receiver),
            "balance": balance,
            "resource": resource,
        }

        return self._build_transaction("UnDelegateResourceContract", payload)

    # Witness

    def create_witness(self, owner: TAddress, url: str) -> "AsyncTransactionBuilder":
        """Create a new witness, will consume 1_000 TRX."""
        payload = {"owner_address": keys.to_hex_address(owner), "url": url.encode().hex()}
        return self._build_transaction("WitnessCreateContract", payload)

    def vote_witness(self, owner: TAddress, *votes: Tuple[TAddress, int]) -> "AsyncTransactionBuilder":
        """Vote for witnesses. Empty ``votes`` to clean voted."""
        votes = [dict(vote_address=keys.to_hex_address(addr), vote_count=count) for addr, count in votes]
        payload = {"owner_address": keys.to_hex_address(owner), "votes": votes}
        return self._build_transaction("VoteWitnessContract", payload)

    def withdraw_rewards(self, owner: TAddress) -> "AsyncTransactionBuilder":
        """Withdraw voting rewards."""
        payload = {"owner_address": keys.to_hex_address(owner)}
        return self._build_transaction("WithdrawBalanceContract", payload)

    # Contract

    def deploy_contract(self, owner: TAddress, contract: AsyncContract) -> "AsyncTransactionBuilder":
        """Deploy a new contract on chain."""
        contract._client = self.client
        contract.owner_address = owner
        contract.origin_address = owner
        contract.contract_address = None

        return contract.deploy()


# noinspection PyBroadException
class AsyncTrx:
    """The Trx(transaction) API."""

    def __init__(self, tron):
        self._tron = tron

    @property
    def client(self) -> "AsyncTron":
        return self._tron

    def _build_transaction(self, type_: str, obj: dict, *, method: AsyncContractMethod = None) -> AsyncTransactionBuilder:
        inner = {
            "parameter": {"value": obj, "type_url": f"type.googleapis.com/protocol.{type_}"},
            "type": type_,
        }
        if method:
            return AsyncTransactionBuilder(inner, client=self.client, method=method)
        return AsyncTransactionBuilder(inner, client=self.client)

    def transfer(self, from_: TAddress, to: TAddress, amount: int) -> AsyncTransactionBuilder:
        """Transfer TRX. ``amount`` in `SUN`."""
        return self._build_transaction(
            "TransferContract",
            {"owner_address": keys.to_hex_address(from_), "to_address": keys.to_hex_address(to), "amount": amount},
        )

    # TRC10 asset

    def asset_transfer(self, from_: TAddress, to: TAddress, amount: int, token_id: int) -> AsyncTransactionBuilder:
        """Transfer TRC10 tokens."""
        return self._build_transaction(
            "TransferAssetContract",
            {
                "owner_address": keys.to_hex_address(from_),
                "to_address": keys.to_hex_address(to),
                "amount": amount,
                "asset_name": str(token_id).encode().hex(),
            },
        )

    def asset_issue(
        self,
        owner: TAddress,
        abbr: str,
        total_supply: int,
        *,
        url: str,
        name: str = None,
        description: str = "",
        start_time: int = None,
        end_time: int = None,
        precision: int = 6,
        frozen_supply: list = None,
        trx_num: int = 1,
        num: int = 1,
    ) -> AsyncTransactionBuilder:
        """Issue a TRC10 token.

        Almost all parameters have resonable defaults.
        """
        if name is None:
            name = abbr

        if start_time is None:
            # use default expiration
            start_time = current_timestamp() + 60_000

        if end_time is None:
            # use default expiration
            end_time = current_timestamp() + 60_000 + 1

        if frozen_supply is None:
            frozen_supply = []

        return self._build_transaction(
            "AssetIssueContract",
            {
                "owner_address": keys.to_hex_address(owner),
                "abbr": abbr.encode().hex(),
                "name": name.encode().hex(),
                "total_supply": total_supply,
                "precision": precision,
                "url": url.encode().hex(),
                "description": description.encode().hex(),
                "start_time": start_time,
                "end_time": end_time,
                "frozen_supply": frozen_supply,
                "trx_num": trx_num,
                "num": num,
                "public_free_asset_net_limit": 0,
                "free_asset_net_limit": 0,
            },
        )

    # Account

    def account_permission_update(self, owner: TAddress, perm: dict) -> "AsyncTransactionBuilder":
        """Update account permission.

        :param owner: Address of owner
        :param perm: Permission dict from :meth:`~tronpy.Tron.get_account_permission`
        """

        if "owner" in perm:
            for key in perm["owner"]["keys"]:
                key["address"] = keys.to_hex_address(key["address"])
        if "actives" in perm:
            for act in perm["actives"]:
                for key in act["keys"]:
                    key["address"] = keys.to_hex_address(key["address"])
        if perm.get("witness", None):
            for key in perm["witness"]["keys"]:
                key["address"] = keys.to_hex_address(key["address"])

        return self._build_transaction(
            "AccountPermissionUpdateContract",
            dict(owner_address=keys.to_hex_address(owner), **perm),
        )

    def account_update(self, owner: TAddress, name: str) -> "AsyncTransactionBuilder":
        """Update account name. An account can only set name once."""
        return self._build_transaction(
            "UpdateAccountContract",
            {"owner_address": keys.to_hex_address(owner), "account_name": name.encode().hex()},
        )

    def freeze_balance(self, owner: TAddress, amount: int, resource: str = "ENERGY") -> "AsyncTransactionBuilder":
        """Freeze balance to get energy or bandwidth, for 3 days.

        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        """
        payload = {
            "owner_address": keys.to_hex_address(owner),
            "frozen_balance": amount,
            "resource": resource,
        }
        return self._build_transaction("FreezeBalanceV2Contract", payload)

    def withdraw_stake_balance(self, owner: TAddress) -> "AsyncTransactionBuilder":
        """Withdraw all stake v2 balance after waiting for 14 days since unfreeze_balance call.

        :param owner:
        """
        payload = {
            "owner_address": keys.to_hex_address(owner),
        }
        return self._build_transaction("WithdrawExpireUnfreezeContract", payload)

    def unfreeze_balance(
        self, owner: TAddress, resource: str = "ENERGY", *, unfreeze_balance: int
    ) -> "AsyncTransactionBuilder":
        """Unfreeze balance to get TRX back.

        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        """
        payload = {
            "owner_address": keys.to_hex_address(owner),
            "unfreeze_balance": unfreeze_balance,
            "resource": resource,
        }
        return self._build_transaction("UnfreezeBalanceV2Contract", payload)

    def unfreeze_balance_legacy(
        self, owner: TAddress, resource: str = "ENERGY", receiver: TAddress = None
    ) -> "AsyncTransactionBuilder":
        """Unfreeze balance to get TRX back.

        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        """
        payload = {
            "owner_address": keys.to_hex_address(owner),
            "resource": resource,
        }
        if receiver is not None:
            payload["receiver_address"] = keys.to_hex_address(receiver)
        return self._build_transaction("UnfreezeBalanceContract", payload)

    def delegate_resource(
        self,
        owner: TAddress,
        receiver: TAddress,
        balance: int,
        resource: str = "BANDWIDTH",
        lock: bool = False,
        lock_period: int = None,
    ) -> "AsyncTransactionBuilder":
        """Delegate bandwidth or energy resources to other accounts in Stake2.0.

        :param owner:
        :param receiver:
        :param balance:
        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        :param lock: Optionally lock delegated resources for 3 days.
        :param lock_period: Optionally lock delegated resources for a specific period. Default: 3 days.
        """

        payload = {
            "owner_address": keys.to_hex_address(owner),
            "receiver_address": keys.to_hex_address(receiver),
            "balance": balance,
            "resource": resource,
            "lock": lock,
        }
        if lock_period is not None:
            payload["lock_period"] = lock_period

        return self._build_transaction("DelegateResourceContract", payload)

    def undelegate_resource(
        self, owner: TAddress, receiver: TAddress, balance: int, resource: str = "BANDWIDTH"
    ) -> "AsyncTransactionBuilder":
        """Cancel the delegation of bandwidth or energy resources to other accounts in Stake2.0

        :param owner:
        :param receiver:
        :param balance:
        :param resource: Resource type, can be ``"ENERGY"`` or ``"BANDWIDTH"``
        """

        payload = {
            "owner_address": keys.to_hex_address(owner),
            "receiver_address": keys.to_hex_address(receiver),
            "balance": balance,
            "resource": resource,
        }

        return self._build_transaction("UnDelegateResourceContract", payload)

    # Witness

    def create_witness(self, owner: TAddress, url: str) -> "AsyncTransactionBuilder":
        """Create a new witness, will consume 1_000 TRX."""
        payload = {"owner_address": keys.to_hex_address(owner), "url": url.encode().hex()}
        return self._build_transaction("WitnessCreateContract", payload)

    def vote_witness(self, owner: TAddress, *votes: Tuple[TAddress, int]) -> "AsyncTransactionBuilder":
        """Vote for witnesses. Empty ``votes`` to clean voted."""
        votes = [dict(vote_address=keys.to_hex_address(addr), vote_count=count) for addr, count in votes]
        payload = {"owner_address": keys.to_hex_address(owner), "votes": votes}
        return self._build_transaction("VoteWitnessContract", payload)

    def withdraw_rewards(self, owner: TAddress) -> "AsyncTransactionBuilder":
        """Withdraw voting rewards."""
        payload = {"owner_address": keys.to_hex_address(owner)}
        return self._build_transaction("WithdrawBalanceContract", payload)

    # Contract

    def deploy_contract(self, owner: TAddress, contract: AsyncContract) -> "AsyncTransactionBuilder":
        """Deploy a new contract on chain."""
        contract._client = self.client
        contract.owner_address = owner
        contract.origin_address = owner
        contract.contract_address = None

        return contract.deploy()


# noinspection PyBroadException
class AsyncTron:
    """The Async TRON API Client.

    :param provider: An :class:`~tronpy.providers.HTTPProvider` object, can be configured to use private node
    :param network: Which network to connect, one of ``"mainnet"``, ``"shasta"``, ``"nile"``, or ``"tronex"``
    """

    # Address API
    is_address = staticmethod(keys.is_address)
    """Is object a TRON address, both hex format and base58check format."""

    is_base58check_address = staticmethod(keys.is_base58check_address)
    """Is object an address in base58check format."""

    is_hex_address = staticmethod(keys.is_hex_address)
    """Is object an address in hex str format."""

    to_base58check_address = staticmethod(keys.to_base58check_address)
    """Convert address of any format to a base58check format."""

    to_hex_address = staticmethod(keys.to_hex_address)
    """Convert address of any format to a hex format."""

    to_canonical_address = staticmethod(keys.to_base58check_address)

    def __init__(self, provider: AsyncHTTPProvider = None, *, network: str = "mainnet", conf: dict = None):
        self.conf = DEFAULT_CONF
        """The config dict."""

        if conf is not None:
            self.conf = dict(DEFAULT_CONF, **conf)

        if provider is None:
            self.provider = AsyncHTTPProvider(conf_for_name(network), self.conf["timeout"])
        elif isinstance(provider, (AsyncHTTPProvider,)):
            self.provider = provider
        else:
            raise TypeError("provider is not a HTTPProvider")

        self._trx = AsyncTrx(self)

    @property
    def trx(self) -> AsyncTrx:
        """
        Helper object to send various transactions.

        :type: Trx
        """
        return self._trx

    def _handle_api_error(self, payload: dict):
        if payload.get("result", None) is True:
            return
        if "Error" in payload:
            # class java.lang.NullPointerException : null
            raise ApiError(payload["Error"])
        if "code" in payload:
            try:
                msg = bytes.fromhex(payload["message"]).decode()
            except Exception:
                msg = payload.get("message", str(payload))

            if payload["code"] == "SIGERROR":
                raise BadSignature(msg)
            elif payload["code"] == "TAPOS_ERROR":
                raise TaposError(msg)
            elif payload["code"] in ["TRANSACTION_EXPIRATION_ERROR", "TOO_BIG_TRANSACTION_ERROR"]:
                raise TransactionError(msg)
            elif payload["code"] == "CONTRACT_VALIDATE_ERROR":
                raise ValidationError(msg)
            raise UnknownError(msg, payload["code"])
        if "result" in payload and isinstance(payload["result"], (dict,)):
            return self._handle_api_error(payload["result"])

    # Address utilities

    def generate_address(self, priv_key=None) -> dict:
        """Generate a random address."""
        if priv_key is None:
            priv_key = PrivateKey.random()
        return {
            "base58check_address": priv_key.public_key.to_base58check_address(),
            "hex_address": priv_key.public_key.to_hex_address(),
            "private_key": priv_key.hex(),
            "public_key": priv_key.public_key.hex(),
        }

    def generate_address_from_mnemonic(self, mnemonic: str, passphrase: str = "", account_path: str = TRON_DEFAULT_PATH):
        """
        Generate address from a mnemonic.

        :param str mnemonic: space-separated list of BIP39 mnemonic seed words
        :param str passphrase: Optional passphrase used to encrypt the mnemonic
        :param str account_path: Specify an alternate HD path for deriving the seed using
            BIP32 HD wallet key derivation.
        """
        seed = seed_from_mnemonic(mnemonic, passphrase)
        key = key_from_seed(seed, account_path)
        priv_key = PrivateKey(key)
        return {
            "base58check_address": priv_key.public_key.to_base58check_address(),
            "hex_address": priv_key.public_key.to_hex_address(),
            "private_key": priv_key.hex(),
            "public_key": priv_key.public_key.hex(),
        }

    def generate_address_with_mnemonic(
        self, passphrase: str = "", num_words: int = 12, language: str = "english", account_path: str = TRON_DEFAULT_PATH
    ):
        r"""
        Create a new address and related mnemonic.

        Creates a new address, and returns it alongside the mnemonic that can be used to regenerate it using any BIP39-compatible wallet.

        :param str passphrase: Extra passphrase to encrypt the seed phrase
        :param int num_words: Number of words to use with seed phrase. Default is 12 words.
                              Must be one of [12, 15, 18, 21, 24].
        :param str language: Language to use for BIP39 mnemonic seed phrase.
        :param str account_path: Specify an alternate HD path for deriving the seed using
            BIP32 HD wallet key derivation.
        """  # noqa: E501
        mnemonic = generate_mnemonic(num_words, language)
        return self.generate_address_from_mnemonic(mnemonic, passphrase, account_path), mnemonic

    def get_address_from_passphrase(self, passphrase: str) -> dict:
        """Get an address from a passphrase, compatiable with `wallet/createaddress`."""
        priv_key = PrivateKey.from_passphrase(passphrase.encode())
        return self.generate_address(priv_key)

    async def generate_zkey(self) -> dict:
        """Generate a random shielded address."""
        return await self.provider.make_request("wallet/getnewshieldedaddress")

    async def get_zkey_from_sk(self, sk: str, d: str = None) -> dict:
        """Get the shielded address from sk(spending key) and d(diversifier)."""
        if len(sk) != 64:
            raise BadKey("32 byte sk required")
        if d and len(d) != 22:
            raise BadKey("11 byte d required")

        esk = await self.provider.make_request("wallet/getexpandedspendingkey", {"value": sk})
        ask = esk["ask"]
        nsk = esk["nsk"]
        ovk = esk["ovk"]
        ak = (await self.provider.make_request("wallet/getakfromask", {"value": ask}))["value"]
        nk = (await self.provider.make_request("wallet/getnkfromnsk", {"value": nsk}))["value"]

        ivk = (await self.provider.make_request("wallet/getincomingviewingkey", {"ak": ak, "nk": nk}))["ivk"]

        if d is None:
            d = (await self.provider.make_request("wallet/getdiversifier"))["d"]

        ret = await self.provider.make_request("wallet/getzenpaymentaddress", {"ivk": ivk, "d": d})
        pkD = ret["pkD"]
        payment_address = ret["payment_address"]

        return dict(
            sk=sk,
            ask=ask,
            nsk=nsk,
            ovk=ovk,
            ak=ak,
            nk=nk,
            ivk=ivk,
            d=d,
            pkD=pkD,
            payment_address=payment_address,
        )

    # Account query
    async def get_account(self, addr: TAddress) -> dict:
        """Get account info from an address."""

        ret = await self.provider.make_request(
            "wallet/getaccount", {"address": keys.to_base58check_address(addr), "visible": True}
        )
        if ret:
            return ret
        else:
            raise AddressNotFound("account not found on-chain")

    # Bandwidth query
    async def get_bandwidth(self, addr: TAddress) -> int:
        """Query the bandwidth of the account"""

        ret = await self.provider.make_request(
            "wallet/getaccountnet", {"address": keys.to_base58check_address(addr), "visible": True}
        )
        if ret:
            # (freeNetLimit - freeNetUsed) + (NetLimit - NetUsed)
            return ret["freeNetLimit"] - ret.get("freeNetUsed", 0) + ret.get("NetLimit", 0) - ret.get("NetUsed", 0)
        else:
            raise AddressNotFound("account not found on-chain")

    async def get_energy(self, address: str) -> int:
        """Query the energy of the account"""
        account_info = await self.get_account_resource(address)
        energy_limit = account_info.get("EnergyLimit", 0)
        energy_used = account_info.get("EnergyUsed", 0)
        return energy_limit - energy_used

    async def get_account_resource(self, addr: TAddress) -> dict:
        """Get resource info of an account."""

        ret = await self.provider.make_request(
            "wallet/getaccountresource",
            {"address": keys.to_base58check_address(addr), "visible": True},
        )
        if ret:
            return ret
        else:
            raise AddressNotFound("account not found on-chain")

    async def get_account_balance(self, addr: TAddress) -> Decimal:
        """Get TRX balance of an account. Result in `TRX`."""

        info = await self.get_account(addr)
        return Decimal(info.get("balance", 0)) / 1_000_000

    async def get_account_asset_balances(self, addr: TAddress) -> dict:
        """Get all TRC10 token balances of an account."""
        info = await self.get_account(addr)
        return {p["key"]: p["value"] for p in info.get("assetV2", {}) if p["value"] > 0}

    async def get_account_asset_balance(self, addr: TAddress, token_id: Union[int, str]) -> int:
        """Get TRC10 token balance of an account. Result is in raw amount."""
        if int(token_id) < 1000000 or int(token_id) > 1999999:
            raise ValueError("invalid token_id range")

        balances = await self.get_account_asset_balances(addr)
        return balances.get(str(token_id), 0)

    async def get_account_permission(self, addr: TAddress) -> dict:
        """Get account's permission info from an address. Can be used in `account_permission_update`."""

        addr = keys.to_base58check_address(addr)
        # will check account existence
        info = await self.get_account(addr)
        # For old accounts prior to AccountPermissionUpdate, these fields are not set.
        # So default permission is for backward compatibility.
        default_witness = None
        if info.get("is_witness", None):
            default_witness = {
                "type": "Witness",
                "id": 1,
                "permission_name": "witness",
                "threshold": 1,
                "keys": [{"address": addr, "weight": 1}],
            }
        return {
            "owner": info.get(
                "owner_permission",
                {"permission_name": "owner", "threshold": 1, "keys": [{"address": addr, "weight": 1}]},
            ),
            "actives": info.get(
                "active_permission",
                [
                    {
                        "type": "Active",
                        "id": 2,
                        "permission_name": "active",
                        "threshold": 1,
                        "operations": "7fff1fc0033e0100000000000000000000000000000000000000000000000000",
                        "keys": [{"address": addr, "weight": 1}],
                    }
                ],
            ),
            "witness": info.get("witness_permission", default_witness),
        }

    async def get_delegated_resource_v2(self, fromAddr: TAddress, toAddr: TAddress) -> dict:
        """Query the amount of delegatable resources share of the specified resource type for an address"""
        return await self.provider.make_request(
            "wallet/getdelegatedresourcev2",
            {
                "fromAddress": keys.to_base58check_address(fromAddr),
                "toAddress": keys.to_base58check_address(toAddr),
                "visible": True,
            },
        )

    async def get_delegated_resource_account_index_v2(self, addr: TAddress) -> dict:
        """Query the resource delegation index by an account"""
        return await self.provider.make_request(
            "wallet/getdelegatedresourceaccountindexv2",
            {
                "value": keys.to_base58check_address(addr),
                "visible": True,
            },
        )

    # Block query

    async def get_latest_solid_block(self) -> dict:
        return await self.provider.make_request("walletsolidity/getnowblock")

    async def get_latest_solid_block_id(self) -> str:
        """Get latest solid block id in hex."""

        try:
            info = await self.provider.make_request("wallet/getnodeinfo")
            return info["solidityBlock"].split(",ID:", 1)[-1]
        except Exception:
            info = await self.get_latest_solid_block()
            return info["blockID"]

    async def get_latest_solid_block_number(self) -> int:
        """Get latest solid block number. Implemented via `wallet/getnodeinfo`,
        which is faster than `walletsolidity/getnowblock`."""
        info = await self.provider.make_request("wallet/getnodeinfo")
        return int(info["solidityBlock"].split(",ID:", 1)[0].replace("Num:", "", 1))

    async def get_latest_block(self) -> dict:
        """Get latest block."""
        return await self.provider.make_request("wallet/getnowblock", {"visible": True})

    async def get_latest_block_id(self) -> str:
        """Get latest block id in hex."""

        info = await self.provider.make_request("wallet/getnodeinfo")
        return info["block"].split(",ID:", 1)[-1]

    async def get_latest_block_number(self) -> int:
        """Get latest block number. Implemented via `wallet/getnodeinfo`, which is faster than `wallet/getnowblock`."""

        info = await self.provider.make_request("wallet/getnodeinfo")
        return int(info["block"].split(",ID:", 1)[0].replace("Num:", "", 1))

    async def get_block(self, id_or_num: Union[None, str, int] = None, *, visible: bool = True) -> dict:
        """Get block from a block id or block number.

        :param id_or_num: Block number, or Block hash(id), or ``None`` (default) to get the latest block.
        :param visible: Use ``visible=False`` to get non-base58check addresses and strings instead of hex strings.
        """

        if isinstance(id_or_num, (int,)):
            block = await self.provider.make_request("wallet/getblockbynum", {"num": id_or_num, "visible": visible})
        elif isinstance(id_or_num, (str,)):
            block = await self.provider.make_request("wallet/getblockbyid", {"value": id_or_num, "visible": visible})
        elif id_or_num is None:
            block = await self.provider.make_request("wallet/getnowblock", {"visible": visible})
        else:
            raise TypeError(f"can not infer type of {id_or_num}")

        if "Error" in (block or {}):
            raise BugInJavaTron(block)
        elif block:
            return block
        else:
            raise BlockNotFound

    async def get_transaction(self, txn_id: str) -> dict:
        """Get transaction from a transaction id."""

        if len(txn_id) != 64:
            raise BadHash("wrong transaction hash length")

        ret = await self.provider.make_request("wallet/gettransactionbyid", {"value": txn_id, "visible": True})
        self._handle_api_error(ret)
        if ret:
            return ret
        raise TransactionNotFound

    async def get_solid_transaction(self, txn_id: str) -> dict:
        """Get transaction from a transaction id, must be in solid block."""

        if len(txn_id) != 64:
            raise BadHash("wrong transaction hash length")

        ret = await self.provider.make_request("walletsolidity/gettransactionbyid", {"value": txn_id, "visible": True})
        self._handle_api_error(ret)
        if ret:
            return ret
        raise TransactionNotFound

    async def get_transaction_info(self, txn_id: str) -> dict:
        """Get transaction receipt info from a transaction id."""

        if len(txn_id) != 64:
            raise BadHash("wrong transaction hash length")

        ret = await self.provider.make_request("wallet/gettransactioninfobyid", {"value": txn_id, "visible": True})
        self._handle_api_error(ret)
        if ret:
            return ret
        raise TransactionNotFound

    async def get_solid_transaction_info(self, txn_id: str) -> dict:
        """Get transaction receipt info from a transaction id, must be in solid block."""

        if len(txn_id) != 64:
            raise BadHash("wrong transaction hash length")

        ret = await self.provider.make_request("walletsolidity/gettransactioninfobyid", {"value": txn_id, "visible": True})
        self._handle_api_error(ret)
        if ret:
            return ret
        raise TransactionNotFound

    # Chain parameters

    async def list_witnesses(self) -> list:
        """List all witnesses, including SR, SRP, and SRC."""
        # NOTE: visible parameter is ignored
        ret = await self.provider.make_request("wallet/listwitnesses", {"visible": True})
        witnesses = ret.get("witnesses", [])
        for witness in witnesses:
            witness["address"] = keys.to_base58check_address(witness["address"])

        return witnesses

    async def list_nodes(self) -> list:
        """List all nodes that current API node is connected to."""
        # NOTE: visible parameter is ignored
        ret = await self.provider.make_request("wallet/listnodes", {"visible": True})
        nodes = ret.get("nodes", [])
        for node in nodes:
            node["address"]["host"] = bytes.fromhex(node["address"]["host"]).decode()
        return nodes

    async def get_node_info(self) -> dict:
        """Get current API node' info."""

        return await self.provider.make_request("wallet/getnodeinfo", {"visible": True})

    async def get_chain_parameters(self) -> dict:
        """List all chain parameters, values that can be changed via proposal."""
        params = await self.provider.make_request("wallet/getchainparameters", {"visible": True})
        return params.get("chainParameter", [])

    # Asset (TRC10)

    async def get_asset(self, id: int = None, issuer: TAddress = None) -> dict:
        """Get TRC10(asset) info by asset's id or issuer."""
        if id and issuer:
            raise ValueError("either query by id or issuer")
        if id:
            return await self.provider.make_request("wallet/getassetissuebyid", {"value": id, "visible": True})
        else:
            return await self.provider.make_request(
                "wallet/getassetissuebyaccount",
                {"address": keys.to_base58check_address(issuer), "visible": True},
            )

    async def get_asset_from_name(self, name: str) -> dict:
        """Get asset info from its abbr name, might fail if there're duplicates."""
        assets = [asset for asset in await self.list_assets() if asset["abbr"] == name]
        if assets:
            if len(assets) == 1:
                return assets[0]
            raise ValueError("duplicated assets with the same name", [asset["id"] for asset in assets])
        raise AssetNotFound

    async def list_assets(self) -> list:
        """List all TRC10 tokens(assets)."""
        ret = await self.provider.make_request("wallet/getassetissuelist", {"visible": True})
        assets = ret["assetIssue"]
        for asset in assets:
            asset["id"] = int(asset["id"])
            asset["owner_address"] = keys.to_base58check_address(asset["owner_address"])
            asset["name"] = bytes.fromhex(asset["name"]).decode()
            if "abbr" in asset:
                asset["abbr"] = bytes.fromhex(asset["abbr"]).decode()
            else:
                asset["abbr"] = ""
            asset["description"] = bytes.fromhex(asset["description"]).decode("utf8", "replace")
            asset["url"] = bytes.fromhex(asset["url"]).decode()
        return assets

    # Smart contract

    async def get_contract(self, addr: TAddress) -> AsyncContract:
        """Get a contract object."""
        addr = keys.to_base58check_address(addr)
        info = await self.provider.make_request("wallet/getcontract", {"value": addr, "visible": True})

        try:
            self._handle_api_error(info)
        except ApiError:
            # your java's null pointer exception sucks
            raise AddressNotFound("contract address not found")

        cntr = AsyncContract(
            addr=addr,
            bytecode=info.get("bytecode", ""),
            name=info.get("name", ""),
            abi=info.get("abi", {}).get("entrys", []),
            origin_energy_limit=info.get("origin_energy_limit", 0),
            user_resource_percent=info.get("consume_user_resource_percent", 100),
            origin_address=info.get("origin_address", ""),
            code_hash=info.get("code_hash", ""),
            client=self,
        )
        return cntr

    async def get_contract_info(self, addr: TAddress) -> dict:
        """Queries a contract's information from the blockchain"""
        addr = keys.to_base58check_address(addr)
        info = await self.provider.make_request("wallet/getcontractinfo", {"value": addr, "visible": True})

        try:
            self._handle_api_error(info)
        except ApiError:
            raise AddressNotFound("contract address not found")

        return info

    async def get_contract_as_shielded_trc20(self, addr: TAddress) -> ShieldedTRC20:
        """Get a Shielded TRC20 Contract object."""
        contract = await self.get_contract(addr)
        return ShieldedTRC20(contract)

    async def trigger_constant_contract(
        self,
        owner_address: TAddress,
        contract_address: TAddress,
        function_selector: str,
        parameter: str,
    ) -> dict:
        ret = await self.provider.make_request(
            "wallet/triggerconstantcontract",
            {
                "owner_address": keys.to_base58check_address(owner_address),
                "contract_address": keys.to_base58check_address(contract_address),
                "function_selector": function_selector,
                "parameter": parameter,
                "visible": True,
            },
        )
        self._handle_api_error(ret)
        if "message" in ret.get("result", {}):
            msg = ret["result"]["message"]
            result = ret.get("constant_result", [])
            try:
                if result and len(result[0]) > (4 + 32) * 2:
                    error_msg = tron_abi.decode_single("string", bytes.fromhex(result[0])[4 + 32 :])
                    msg = f"{msg}: {error_msg}"
            except Exception:
                pass
            raise TvmError(msg)
        return ret

    async def trigger_const_smart_contract_function(
        self,
        owner_address: TAddress,
        contract_address: TAddress,
        function_selector: str,
        parameter: str,
    ) -> str:
        ret = await self.trigger_constant_contract(owner_address, contract_address, function_selector, parameter)
        return ret["constant_result"][0]

    # Transaction handling

    async def broadcast(self, txn: AsyncTransaction) -> dict:
        payload = await self.provider.make_request("wallet/broadcasttransaction", txn.to_json())
        self._handle_api_error(payload)
        return payload

    async def get_sign_weight(self, txn: AsyncTransaction) -> dict:
        return await self.provider.make_request("wallet/getsignweight", txn.to_json())

    async def get_estimated_energy(
        self,
        owner_address: TAddress,
        contract_address: TAddress,
        function_selector: str,
        parameter: str,
    ) -> int:
        """Returns an estimated energy of calling a contract from the chain."""
        params = {
            "owner_address": keys.to_base58check_address(owner_address),
            "contract_address": keys.to_base58check_address(contract_address),
            "function_selector": function_selector,
            "parameter": parameter,
            "visible": True,
        }
        ret = await self.provider.make_request("wallet/estimateenergy", params)
        self._handle_api_error(ret)
        return ret["energy_required"]

    async def close(self):
        if not self.provider.client.is_closed:
            await self.provider.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.provider.client.aclose()

    async def get_usdt_balance(self, address: str) -> Decimal:
        """查询地址的USDT余额

        Args:
            address: TRON地址

        Returns:
            Decimal: USDT余额
        """
        USDT_CONTRACT_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        contract = await self.get_contract(USDT_CONTRACT_ADDRESS)
        balance = await contract.functions.balanceOf(address)
        # USDT有6位小数
        return Decimal(balance) / Decimal(10 ** 6)

    async def get_token_balance(self, token_address: str, address: str, decimals: int = 18) -> Decimal:
        """查询任意TRC20代币余额

        Args:
            token_address: 代币合约地址
            address: 要查询的地址
            decimals: 代币小数位数

        Returns:
            Decimal: 代币余额
        """
        contract = await self.get_contract(token_address)
        balance = await contract.functions.balanceOf(address)
        return Decimal(balance) / Decimal(10 ** decimals)

    async def get_token_info(self, token_address: str) -> dict:
        """获取代币基本信息

        Args:
            token_address: 代币合约地址

        Returns:
            dict: 包含name, symbol, decimals的字典
        """
        contract = await self.get_contract(token_address)
        name = await contract.functions.name()
        symbol = await contract.functions.symbol()
        decimals = await contract.functions.decimals()
        return {
            "name": name,
            "symbol": symbol,
            "decimals": decimals
        }

    async def batch_get_token_balance(self, token_address: str, addresses: list, decimals: int = 18) -> dict:
        """批量查询多个地址的代币余额

        Args:
            token_address: 代币合约地址
            addresses: 地址列表
            decimals: 代币小数位数

        Returns:
            dict: 地址到余额的映射
        """
        contract = await self.get_contract(token_address)
        balances = {}
        for address in addresses:
            balance = await contract.functions.balanceOf(address)
            balances[address] = Decimal(balance) / Decimal(10 ** decimals)
        return balances

    async def scan_block_transfers(self, start_block: int, end_block: int = None, address: str = None) -> dict:
        """扫描指定区块范围内的TRX和USDT转账记录

        Args:
            start_block: 起始区块
            end_block: 结束区块，如果不指定则扫描到最新区块
            address: 可选，只返回与该地址相关的转账记录

        Returns:
            dict: 包含区块信息和转账记录
            {
                'blocks': [{
                    'block_number': 区块号,
                    'timestamp': 时间戳,
                    'hash': 区块哈希,
                    'parent_hash': 父区块哈希,
                    'witness_address': 出块节点地址,
                    'transaction_count': 交易数量,
                    'confirmed': 是否已确认
                }],
                'transfers': {
                    'trx': [{
                        'block_number': 区块号,
                        'timestamp': 时间戳,
                        'txid': 交易哈希,
                        'from': 发送地址,
                        'to': 接收地址,
                        'amount': 金额(TRX),
                        'resource': {
                            'energy_usage': 能量消耗,
                            'net_usage': 带宽消耗,
                            'energy_fee': 能量手续费,
                            'net_fee': 带宽手续费
                        }
                    }],
                    'usdt': [{
                        'block_number': 区块号,
                        'timestamp': 时间戳,
                        'txid': 交易哈希,
                        'from': 发送地址,
                        'to': 接收地址,
                        'amount': 金额(USDT),
                        'resource': {
                            'energy_usage': 能量消耗,
                            'net_usage': 带宽消耗,
                            'energy_fee': 能量手续费,
                            'net_fee': 带宽手续费
                        }
                    }]
                }
            }
        """
        USDT_CONTRACT_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        if end_block is None:
            end_block = await self.get_latest_block_number()

        result = {
            'blocks': [],
            'transfers': {
                'trx': [],
                'usdt': []
            },
            'stats': {
                'total_transactions': 0,
                'failed_parse': 0,
                'successful_parse': 0,
                'failed_txids': []  # 添加解析失败的交易ID列表
            }
        }

        for block_num in range(start_block, end_block + 1):
            block = await self.get_block(block_num)

            # 添加区块信息
            block_header = block['block_header']['raw_data']
            result['blocks'].append({
                'block_number': block_num,
                'timestamp': block_header['timestamp'],
                'hash': block.get('blockID', ''),
                'parent_hash': block_header.get('parentHash', ''),
                'witness_address': self.to_base58check_address(block_header.get('witness_address', '')),
                'transaction_count': len(block.get('transactions', [])),
                'confirmed': block.get('confirmed', True)
            })

            if not block.get('transactions'):
                continue

            for tx in block['transactions']:
                # 检查TRX转账
                if tx.get('raw_data', {}).get('contract', []):
                    contract = tx['raw_data']['contract'][0]
                    if contract['type'] == 'TransferContract':
                        value = contract['parameter']['value']
                        from_addr = self.to_base58check_address(value['owner_address'])
                        to_addr = self.to_base58check_address(value['to_address'])
                        amount = Decimal(value['amount']) / Decimal(10 ** 6)

                        # 获取资源消耗
                        resource = {}
                        if 'ret' in tx and tx['ret']:
                            receipt = tx['ret'][0]
                            resource = {
                                'energy_usage': receipt.get('energy_usage', 0),
                                'net_usage': receipt.get('net_usage', 0),
                                'energy_fee': receipt.get('energy_fee', 0),
                                'net_fee': receipt.get('net_fee', 0),
                            }

                        if address is None or address in [from_addr, to_addr]:
                            result['transfers']['trx'].append({
                                'block_number': block_num,
                                'timestamp': block_header['timestamp'],
                                'txid': tx['txID'],
                                'from': from_addr,
                                'to': to_addr,
                                'amount': amount,
                                'resource': resource
                            })

                # 检查USDT转账
                if tx.get('raw_data', {}).get('contract', []):
                    contract = tx['raw_data']['contract'][0]
                    if contract['type'] == 'TriggerSmartContract' and \
                       self.to_base58check_address(contract['parameter']['value']['contract_address']) == USDT_CONTRACT_ADDRESS:
                        result['stats']['total_transactions'] += 1
                        transfer_info = await self.parse_usdt_transfer(tx, block_num, block_header['timestamp'])

                        if transfer_info:
                            # 获取资源消耗
                            if 'ret' in tx and tx['ret']:
                                receipt = tx['ret'][0]
                                transfer_info['resource'] = {
                                    'energy_usage': receipt.get('energy_usage', 0),
                                    'net_usage': receipt.get('net_usage', 0),
                                    'energy_fee': receipt.get('energy_fee', 0),
                                    'net_fee': receipt.get('net_fee', 0),
                                }

                            result['stats']['successful_parse'] += 1
                            if address is None or address in [transfer_info['from'], transfer_info['to']]:
                                result['transfers']['usdt'].append(transfer_info)
                        else:
                            result['stats']['failed_parse'] += 1
                            result['stats']['failed_txids'].append(tx['txID'])  # 记录解析失败的交易ID

        return result

    async def scan_recent_transfers(self, block_count: int = 1, address: str = None) -> dict:
        """扫描最近N个区块的TRX和USDT转账记录

        Args:
            block_count: 要扫描的区块数量，例如：
                        1 = 只扫描最新区块
                        10 = 扫描最近10个区块
            address: 可选，只返回与该地址相关的转账记录

        Returns:
            dict: 包含区块信息和转账记录
            {
                'latest_block': 最新区块号,
                'start_block': 起始区块号,
                'blocks': [{
                    'block_number': 区块号,
                    'timestamp': 时间戳,
                    'hash': 区块哈希,
                    'parent_hash': 父区块哈希,
                    'witness_address': 出块节点地址,
                    'transaction_count': 交易数量,
                    'confirmed': 是否已确认
                }],
                'transfers': {
                    'trx': [{
                        'block_number': 区块号,
                        'timestamp': 时间戳,
                        'txid': 交易哈希,
                        'from': 发送地址,
                        'to': 接收地址,
                        'amount': 金额(TRX),
                        'resource': {
                            'energy_usage': 能量消耗,
                            'net_usage': 带宽消耗,
                            'energy_fee': 能量手续费,
                            'net_fee': 带宽手续费
                        }
                    }],
                    'usdt': [{
                        'block_number': 区块号,
                        'timestamp': 时间戳,
                        'txid': 交易哈希,
                        'from': 发送地址,
                        'to': 接收地址,
                        'amount': 金额(USDT),
                        'resource': {
                            'energy_usage': 能量消耗,
                            'net_usage': 带宽消耗,
                            'energy_fee': 能量手续费,
                            'net_fee': 带宽手续费
                        }
                    }]
                }
            }
        """
        latest_block = await self.get_latest_block_number()
        start_block = max(latest_block - block_count + 1, 0)  # 确保不会小于0

        result = await self.scan_block_transfers(start_block, latest_block, address)
        result['latest_block'] = latest_block
        result['start_block'] = start_block

        return result

    async def parse_usdt_transfer(self, tx: Dict[str, Any], block_num: int, block_timestamp: int) -> Optional[Dict[str, Any]]:
        """解析USDT转账事件

        Args:
            tx: 交易数据
            block_num: 区块号
            block_timestamp: 区块时间戳

        Returns:
            Optional[Dict[str, Any]]: 解析后的转账信息，解析失败返回None
        """
        try:
            contract = tx['raw_data']['contract'][0]
            value = contract['parameter']['value']

            # 解析合约调用数据
            data = value.get('data', '')

            # transfer方法
            if data.startswith('a9059cbb'):
                try:
                    to_addr = self.to_base58check_address('41' + data[32:72])
                    amount = int(data[72:], 16) / 10 ** 6  # USDT精度是6
                    from_addr = self.to_base58check_address(value['owner_address'])
                except Exception as e:
                    return None

            # transferFrom方法
            elif data.startswith('23b872dd'):
                try:
                    from_addr = self.to_base58check_address('41' + data[32:72])
                    to_addr = self.to_base58check_address('41' + data[72:112])
                    amount = int(data[112:], 16) / 10 ** 6  # USDT精度是6
                except Exception as e:
                    return None
            else:
                return None  # 不是transfer或transferFrom方法

            return {
                'block_number': block_num,
                'timestamp': block_timestamp,
                'txid': tx['txID'],
                'from': from_addr,
                'to': to_addr,
                'amount': amount,
                'method': 'transfer' if data.startswith('a9059cbb') else 'transferFrom'
            }

        except Exception:
            return


