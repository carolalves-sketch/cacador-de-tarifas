"""Carrega o config.yaml e as chaves de API (variaveis de ambiente)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parent.parent
CAMINHO_CONFIG = RAIZ / "config.yaml"
PASTA_DADOS = RAIZ / "data"
PASTA_PAINEL = RAIZ / "docs"
PASTA_LOGS = RAIZ / "logs"


@dataclass
class Chaves:
    """Segredos. Nunca ficam no repositorio - vem do ambiente."""

    serpapi: str = ""
    travelpayouts: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def do_ambiente(cls) -> "Chaves":
        return cls(
            serpapi=os.environ.get("SERPAPI_KEY", "").strip(),
            travelpayouts=os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip(),
            telegram_token=os.environ.get("TELEGRAM_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        )

    def faltando(self) -> list[str]:
        nomes = {
            "SERPAPI_KEY": self.serpapi,
            "TRAVELPAYOUTS_TOKEN": self.travelpayouts,
            "TELEGRAM_TOKEN": self.telegram_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
        }
        return [n for n, v in nomes.items() if not v]


@dataclass
class Config:
    bruto: dict[str, Any] = field(default_factory=dict)
    chaves: Chaves = field(default_factory=Chaves)

    # ---------- carregamento ----------
    @classmethod
    def carregar(cls, caminho: Path | None = None) -> "Config":
        caminho = caminho or CAMINHO_CONFIG
        with open(caminho, "r", encoding="utf-8") as f:
            bruto = yaml.safe_load(f)
        cfg = cls(bruto=bruto, chaves=Chaves.do_ambiente())
        cfg.validar()
        return cfg

    def validar(self) -> None:
        pesos = self.bruto["score"]
        soma = sum(v for k, v in pesos.items() if k.startswith("peso_"))
        if soma != 100:
            raise ValueError(
                f"Os pesos do score somam {soma}, e precisam somar 100. "
                "Confira a secao 'score' do config.yaml."
            )
        if self.partida_de > self.partida_ate:
            raise ValueError("partida_de nao pode ser depois de partida_ate.")

    # ---------- atalhos de leitura ----------
    def __getitem__(self, chave: str) -> Any:
        return self.bruto[chave]

    @property
    def origens(self) -> list[dict]:
        return sorted(self.bruto["origens"], key=lambda o: o["prioridade"])

    @property
    def codigos_origem(self) -> list[str]:
        return [o["codigo"] for o in self.origens]

    @property
    def destinos(self) -> list[dict]:
        return self.bruto["destinos"]

    @property
    def codigos_destino(self) -> list[str]:
        return [d["codigo"] for d in self.destinos]

    @property
    def somente_ida(self) -> bool:
        return bool(self.bruto["viagem"]["somente_ida"])

    @property
    def partida_de(self) -> date:
        return date.fromisoformat(str(self.bruto["viagem"]["partida_de"]))

    @property
    def partida_ate(self) -> date:
        return date.fromisoformat(str(self.bruto["viagem"]["partida_ate"]))

    def datas_janela(self) -> list[date]:
        """As datas de partida dentro da janela oficial."""
        dias = (self.partida_ate - self.partida_de).days
        return [self.partida_de + timedelta(days=i) for i in range(dias + 1)]

    def datas_com_margem(self) -> list[date]:
        """Janela + a margem observada de graca pela camada gratuita."""
        margem = int(self.bruto["viagem"].get("margem_fora_janela", 0))
        inicio = self.partida_de - timedelta(days=margem)
        fim = self.partida_ate + timedelta(days=margem)
        return [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]

    def nome_aeroporto(self, codigo: str) -> str:
        for lista in (self.origens, self.destinos):
            for item in lista:
                if item["codigo"] == codigo:
                    return item.get("nome", codigo)
        return codigo

    def custo_deslocamento(self, codigo_origem: str) -> float:
        for o in self.origens:
            if o["codigo"] == codigo_origem:
                return float(o.get("custo_deslocamento", 0))
        return 0.0

    def dentro_da_janela(self, d: date) -> bool:
        return self.partida_de <= d <= self.partida_ate

    @property
    def classe_google(self) -> int:
        mapa = {"economica": 1, "premium": 2, "executiva": 3, "primeira": 4}
        return mapa.get(str(self.bruto["viagem"].get("classe", "economica")).lower(), 1)
