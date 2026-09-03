"""Coletor do Google Flights via SerpApi (camada de confirmacao).

Traz preco real em reais, companhia, escalas, duracao, bagagem e link.
Uma unica busca cobre varias origens e varios destinos: tanto departure_id
quanto arrival_id aceitam codigos separados por virgula.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import requests

from ..db import Oferta, agora_br

URL = "https://serpapi.com/search"
TEMPO_LIMITE = 60

log = logging.getLogger(__name__)


class ErroFonte(Exception):
    """Falha recuperavel de uma fonte de dados."""


class GoogleFlights:
    def __init__(self, chave: str, moeda: str = "BRL") -> None:
        if not chave:
            raise ErroFonte("SERPAPI_KEY nao configurada.")
        self.chave = chave
        self.moeda = moeda
        self.sessao = requests.Session()

    # --------------------------------------------------------------
    def buscar(
        self,
        origens: list[str],
        destinos: list[str],
        data_partida: date,
        data_volta: date | None = None,
        adultos: int = 1,
        classe: int = 1,
        max_escalas: int | None = None,
    ) -> tuple[list[Oferta], dict[str, Any]]:
        """Uma busca. Devolve (ofertas, price_insights)."""
        params = {
            "engine": "google_flights",
            "api_key": self.chave,
            "departure_id": ",".join(origens),
            "arrival_id": ",".join(destinos),
            "outbound_date": data_partida.isoformat(),
            "currency": self.moeda,
            "hl": "pt-br",
            "gl": "br",
            "adults": adultos,
            "travel_class": classe,
            "deep_search": "true",
        }
        if data_volta is None:
            params["type"] = 2                      # somente ida
        else:
            params["type"] = 1                      # ida e volta
            params["return_date"] = data_volta.isoformat()
        if max_escalas == 0:
            params["stops"] = 1                     # 1 = somente voos diretos
        elif max_escalas == 1:
            params["stops"] = 2                     # 2 = ate 1 escala

        dados = self._chamar(params)
        ofertas: list[Oferta] = []
        for bloco in ("best_flights", "other_flights"):
            for item in dados.get(bloco, []) or []:
                oferta = self._converter(item, data_partida, data_volta)
                if oferta:
                    ofertas.append(oferta)
        return ofertas, dados.get("price_insights") or {}

    # --------------------------------------------------------------
    def _chamar(self, params: dict) -> dict:
        try:
            resp = self.sessao.get(URL, params=params, timeout=TEMPO_LIMITE)
        except requests.RequestException as e:
            raise ErroFonte(f"Falha de rede ao chamar o SerpApi: {e}") from e

        if resp.status_code == 401:
            raise ErroFonte("SerpApi recusou a chave (401). Confira o segredo SERPAPI_KEY.")
        if resp.status_code == 429:
            raise ErroFonte("Cota do SerpApi esgotada ou excesso de chamadas (429).")
        if resp.status_code >= 400:
            raise ErroFonte(f"SerpApi respondeu {resp.status_code}: {resp.text[:200]}")

        try:
            dados = resp.json()
        except ValueError as e:
            raise ErroFonte("SerpApi devolveu uma resposta que nao e JSON.") from e

        if dados.get("error"):
            # "Google Flights hasn't returned any results" e comum e nao e falha.
            mensagem = str(dados["error"])
            if "hasn't returned any results" in mensagem or "no results" in mensagem.lower():
                log.info("Sem resultados para esta combinacao: %s", mensagem)
                return {}
            raise ErroFonte(f"SerpApi: {mensagem}")
        return dados

    # --------------------------------------------------------------
    def _converter(self, item: dict, data_partida: date, data_volta: date | None) -> Oferta | None:
        pernas = item.get("flights") or []
        if not pernas:
            return None
        preco = item.get("price")
        if not preco:
            return None

        # Somente a ida: quando a busca e de ida e volta, o Google devolve as
        # pernas das duas direcoes juntas. Cortamos na primeira volta ao Brasil.
        primeira = pernas[0]
        ultima = pernas[-1]

        companhias = []
        for p in pernas:
            nome = (p.get("airline") or "").strip()
            if nome and nome not in companhias:
                companhias.append(nome)

        escalas = item.get("layovers") or []
        descricao_conexoes = ", ".join(
            f"{c.get('id') or c.get('name', '?')} {formatar_minutos(c.get('duration', 0))}"
            for c in escalas
        )
        conexao_menor = min((int(c.get("duration") or 0) for c in escalas), default=0)

        numeros = [str(p.get("flight_number") or "") for p in pernas if p.get("flight_number")]

        return Oferta(
            coletado_em=agora_br().isoformat(timespec="seconds"),
            fonte="google_flights",
            origem=(primeira.get("departure_airport") or {}).get("id", ""),
            destino=(ultima.get("arrival_airport") or {}).get("id", ""),
            data_partida=data_partida.isoformat(),
            data_volta=data_volta.isoformat() if data_volta else "",
            preco=float(preco),
            moeda=self.moeda,
            companhia=companhias[0] if companhias else "",
            companhias="|".join(companhias),
            numero_voo=" ".join(numeros),
            escalas=len(escalas),
            conexoes=descricao_conexoes,
            conexao_min_minutos=conexao_menor,
            duracao_min=int(item.get("total_duration") or 0),
            partida_iso=_iso(primeira.get("departure_airport", {}).get("time")),
            chegada_iso=_iso(ultima.get("arrival_airport", {}).get("time")),
            bagagem=detectar_bagagem(pernas),
            link=montar_link(item, primeira, ultima, data_partida),
        )


# ------------------------------------------------------------------
def detectar_bagagem(pernas: list[dict]) -> str:
    """O Google nao devolve bagagem como campo estruturado; vem em texto."""
    texto = " ".join(
        " ".join(p.get("extensions") or []) + " " + str(p.get("ticket_also_sold_by") or "")
        for p in pernas
    ).lower()
    if any(t in texto for t in ("checked bag", "bagagem despachada", "bagagem incluída", "checked baggage included")):
        if "for a fee" in texto or "paga" in texto:
            return "mao"
        return "despachada"
    if any(t in texto for t in ("carry-on", "bagagem de mão", "cabin bag")):
        return "mao"
    if "basic" in texto or "básica" in texto:
        return "nenhuma"
    return "desconhecida"


def montar_link(item: dict, primeira: dict, ultima: dict, data_partida: date) -> str:
    origem = (primeira.get("departure_airport") or {}).get("id", "")
    destino = (ultima.get("arrival_airport") or {}).get("id", "")
    # Link de busca do Google Voos: sempre valido, mesmo depois que o
    # booking_token expira (os tokens do SerpApi duram poucas horas).
    return (
        "https://www.google.com/travel/flights?q="
        f"Flights%20to%20{destino}%20from%20{origem}%20on%20{data_partida.isoformat()}%20oneway"
    )


def formatar_minutos(minutos: int | None) -> str:
    minutos = int(minutos or 0)
    return f"{minutos // 60}h{minutos % 60:02d}"


def _iso(valor: str | None) -> str:
    """O Google devolve 'YYYY-MM-DD HH:MM'. Normalizamos para ISO."""
    if not valor:
        return ""
    valor = valor.strip().replace(" ", "T")
    try:
        datetime.fromisoformat(valor)
        return valor
    except ValueError:
        return ""
