"""Coletor Travelpayouts / Aviasales (camada gratuita de varredura).

Devolve o menor preco conhecido de cada rota para cada dia do mes, numa
chamada por rota. Os dados vem de cache das buscas do Aviasales (ate 7 dias),
entao servem para APONTAR onde olhar - nunca para disparar alerta sozinhos.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import requests

from ..db import Oferta, agora_br
from .serpapi_flights import ErroFonte

URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
BASE_LINK = "https://www.aviasales.com"
TEMPO_LIMITE = 30

log = logging.getLogger(__name__)


class Travelpayouts:
    def __init__(self, token: str, moeda: str = "brl") -> None:
        if not token:
            raise ErroFonte("TRAVELPAYOUTS_TOKEN nao configurado.")
        self.token = token
        self.moeda = moeda
        self.sessao = requests.Session()

    def buscar_rota(
        self,
        origem: str,
        destino: str,
        mes: str,                 # "2027-04"
        somente_ida: bool = True,
        limite: int = 300,
    ) -> list[Oferta]:
        params = {
            "origin": origem,
            "destination": destino,
            "departure_at": mes,
            "one_way": "true" if somente_ida else "false",
            "currency": self.moeda,
            "sorting": "price",
            "direct": "false",
            "limit": limite,
            "token": self.token,
        }
        try:
            resp = self.sessao.get(URL, params=params, timeout=TEMPO_LIMITE)
        except requests.RequestException as e:
            raise ErroFonte(f"Falha de rede no Travelpayouts: {e}") from e

        if resp.status_code == 401:
            raise ErroFonte("Travelpayouts recusou o token (401).")
        if resp.status_code >= 400:
            raise ErroFonte(f"Travelpayouts respondeu {resp.status_code}: {resp.text[:200]}")

        try:
            corpo = resp.json()
        except ValueError as e:
            raise ErroFonte("Travelpayouts devolveu resposta invalida.") from e

        if not corpo.get("success", True):
            raise ErroFonte(f"Travelpayouts: {corpo.get('error')}")

        ofertas = []
        for item in corpo.get("data", []) or []:
            oferta = self._converter(item, origem, destino)
            if oferta:
                ofertas.append(oferta)
        return ofertas

    # --------------------------------------------------------------
    def _converter(self, item: dict, origem: str, destino: str) -> Oferta | None:
        preco = item.get("price")
        partida = item.get("departure_at")
        if not preco or not partida:
            return None
        try:
            dt = datetime.fromisoformat(partida.replace("Z", "+00:00"))
        except ValueError:
            return None

        duracao = int(item.get("duration_to") or item.get("duration") or 0)
        link = item.get("link") or ""
        if link.startswith("/"):
            link = BASE_LINK + link

        return Oferta(
            coletado_em=agora_br().isoformat(timespec="seconds"),
            fonte="travelpayouts",
            origem=item.get("origin_airport") or origem,
            destino=item.get("destination_airport") or destino,
            data_partida=dt.date().isoformat(),
            preco=float(preco),
            moeda=self.moeda.upper(),
            companhia=item.get("airline") or "",
            companhias=item.get("airline") or "",
            numero_voo=str(item.get("flight_number") or ""),
            escalas=int(item.get("transfers") or 0),
            duracao_min=duracao,
            partida_iso=dt.replace(tzinfo=None).isoformat(timespec="minutes"),
            bagagem="desconhecida",
            link=link,
        )
