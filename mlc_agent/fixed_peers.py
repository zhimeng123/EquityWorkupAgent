from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from mlc_agent.config import load_yaml
from mlc_agent.schemas import CompanyIdentity


class FixedPeer(BaseModel):
    company_name: str = Field(min_length=1)
    stock_code: str = Field(pattern=r"^\d{6}$")
    exchange: str = Field(min_length=1)
    eastmoney_secid: str = Field(min_length=1)
    eastmoney_secu_code: str = Field(pattern=r"^\d{6}\.(SZ|SH|BJ)$")
    xueqiu_symbol: str = Field(pattern=r"^(SZ|SH|BJ)\d{6}$")
    selection_rationale: str = Field(min_length=1)

    def identity(self) -> CompanyIdentity:
        return CompanyIdentity(
            company_name=self.company_name,
            company_short_name=self.company_name,
            stock_code=self.stock_code,
            exchange=self.exchange,
            eastmoney_secid=self.eastmoney_secid,
            eastmoney_secu_code=self.eastmoney_secu_code,
            xueqiu_symbol=self.xueqiu_symbol,
        )


class FixedPeerSet(BaseModel):
    company_name: str = Field(min_length=1)
    peers: list[FixedPeer] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def unique_peer_codes(self) -> "FixedPeerSet":
        codes = [peer.stock_code for peer in self.peers]
        if len(codes) != len(set(codes)):
            raise ValueError("fixed peer stock codes must be unique")
        return self


class FixedPeerConfig(BaseModel):
    version: int = Field(ge=1)
    targets: dict[str, FixedPeerSet]


def load_fixed_peers(config_dir: Path, target_stock_code: str) -> FixedPeerSet:
    config = FixedPeerConfig.model_validate(load_yaml(config_dir / "fixed_peers.yaml"))
    peer_set = config.targets.get(target_stock_code)
    if peer_set is None:
        raise ValueError("fixed peer configuration missing")
    return peer_set
