"""Opportunity Score: transforma uma oferta em uma nota de 0 a 100.

A regra central: o preco vale mais do que tudo, mas com ganho decrescente.
Os demais fatores servem para eliminar itinerarios ruins, nao para promover
voos caros. Ver secao 5 da proposta.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .config import Config
from .db import Historico, Oferta

# Pontos da curva de preco: (desconto, fracao dos pontos de preco)
CURVA = [(0.00, 0.00), (0.05, 0.17), (0.10, 0.33), (0.20, 0.60),
         (0.30, 0.83), (0.40, 0.95), (0.50, 1.00)]


@dataclass
class Baseline:
    valor: float                 # o "preco normal" da celula
    p10: float                   # percentil 10 - usado no detector de erro
    n: int                       # quantas observacoes sustentam o numero
    confianca: str               # "alta" | "media" | "baixa"
    origem_do_calculo: str       # de onde veio, para aparecer no log
    media: float = 0.0
    minimo: float = 0.0


@dataclass
class Avaliacao:
    score: float
    desconto: float
    baseline: Baseline
    partes: dict[str, float] = field(default_factory=dict)
    erro_de_tarifa: bool = False

    @property
    def classificacao(self) -> str:
        if self.score >= 90:
            return "EXCELENTE"
        if self.score >= 75:
            return "BOA"
        if self.score >= 60:
            return "INTERESSANTE"
        return "NORMAL"

    @property
    def emoji(self) -> str:
        return {"EXCELENTE": "\U0001F525", "BOA": "\U0001F7E2",
                "INTERESSANTE": "\U0001F7E1", "NORMAL": "⚪"}[self.classificacao]


# ------------------------------------------------------------------
def interpolar(desconto: float) -> float:
    """Fracao dos pontos de preco correspondente a um desconto."""
    if desconto <= 0:
        return 0.0
    if desconto >= CURVA[-1][0]:
        return 1.0
    for (x0, y0), (x1, y1) in zip(CURVA, CURVA[1:]):
        if x0 <= desconto <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (desconto - x0) / (x1 - x0)
    return 1.0


# ------------------------------------------------------------------
def calcular_baseline(
    hist: Historico,
    cfg: Config,
    oferta: Oferta,
    faixa_tipica_google: tuple[float, float] | None = None,
) -> Baseline:
    """Descobre o preco normal desta celula, em cascata.

    1. historico da propria celula (origem+destino+data)
    2. historico da rota inteira (todas as datas)
    3. faixa tipica informada pelo Google
    4. orcamento de referencia x 1,35
    """
    janela = int(cfg["historico"]["janela_dias"])
    minimo_obs = int(cfg["historico"]["minimo_observacoes"])

    celula = hist.estatisticas(
        hist.precos(oferta.origem, oferta.destino, oferta.data_partida, janela)
    )
    if celula["n"] >= minimo_obs:
        return Baseline(
            valor=celula["mediana"], p10=celula["p10"], n=celula["n"],
            confianca="alta" if celula["n"] >= minimo_obs * 3 else "media",
            origem_do_calculo="historico da data", media=celula["media"],
            minimo=celula["minimo"],
        )

    rota = hist.estatisticas(hist.precos(oferta.origem, oferta.destino, janela_dias=janela))
    if rota["n"] >= minimo_obs:
        return Baseline(
            valor=rota["mediana"], p10=rota["p10"], n=rota["n"],
            confianca="media" if rota["n"] >= minimo_obs * 4 else "baixa",
            origem_do_calculo="historico da rota", media=rota["media"],
            minimo=rota["minimo"],
        )

    if faixa_tipica_google:
        baixo, alto = faixa_tipica_google
        if baixo and alto:
            valor = (float(baixo) + float(alto)) / 2
            return Baseline(
                valor=valor, p10=float(baixo), n=0, confianca="baixa",
                origem_do_calculo="faixa tipica do Google", media=valor,
                minimo=float(baixo),
            )

    referencia = float(cfg["orcamento"]["referencia"]) * 1.35
    return Baseline(
        valor=referencia, p10=referencia * 0.75, n=0, confianca="baixa",
        origem_do_calculo="orcamento de referencia", media=referencia,
        minimo=referencia * 0.75,
    )


# ------------------------------------------------------------------
def pontuar(
    oferta: Oferta,
    baseline: Baseline,
    cfg: Config,
    duracao_mediana_rota: float | None = None,
) -> Avaliacao:
    pesos = cfg["score"]
    prefs = cfg["preferencias"]
    partes: dict[str, float] = {}

    # --- preco ---------------------------------------------------
    desconto = 0.0
    if baseline.valor > 0:
        desconto = 1 - (oferta.preco / baseline.valor)
    partes["preco"] = interpolar(desconto) * pesos["peso_preco"]

    # --- escalas -------------------------------------------------
    tabela_escalas = {0: 1.00, 1: 0.75, 2: 0.33}
    partes["escalas"] = tabela_escalas.get(oferta.escalas, 0.0) * pesos["peso_escalas"]

    # --- duracao -------------------------------------------------
    if duracao_mediana_rota and oferta.duracao_min:
        excesso = oferta.duracao_min / duracao_mediana_rota - 1
        if excesso <= 0:
            fator = 1.0
        elif excesso <= 0.20:
            fator = 0.70
        elif excesso <= 0.40:
            fator = 0.40
        else:
            fator = 0.0
    else:
        fator = 0.70          # sem referencia ainda: nota neutra
    partes["duracao"] = fator * pesos["peso_duracao"]

    # --- horarios ------------------------------------------------
    partes["horarios"] = _pontos_horario(oferta, prefs) * pesos["peso_horarios"]

    # --- bagagem -------------------------------------------------
    tabela_bagagem = {"despachada": 1.0, "mao": 0.6, "desconhecida": 0.5, "nenhuma": 0.0}
    partes["bagagem"] = tabela_bagagem.get(oferta.bagagem, 0.5) * pesos["peso_bagagem"]

    # --- qualidade do itinerario ---------------------------------
    tradicionais = {c.lower() for c in prefs.get("companhias_tradicionais", [])}
    cias = [c.strip() for c in (oferta.companhias or oferta.companhia).split("|") if c.strip()]
    if not cias:
        fator_iti = 0.5
    elif len(cias) > 2:
        fator_iti = 0.2                     # itinerario remendado
    elif all(c.lower() in tradicionais for c in cias):
        fator_iti = 1.0
    elif any(c.lower() in tradicionais for c in cias):
        fator_iti = 0.6
    else:
        fator_iti = 0.4
    partes["itinerario"] = fator_iti * pesos["peso_itinerario"]

    score = sum(partes.values())

    # --- penalidade de conexao ruim ------------------------------
    if oferta.escalas and oferta.conexao_min_minutos:
        minima = int(prefs["conexao_min_minutos"])
        maxima = int(prefs["conexao_max_horas"]) * 60
        if oferta.conexao_min_minutos < minima or oferta.conexao_min_minutos > maxima:
            score -= 3
            partes["penalidade_conexao"] = -3.0

    score = max(0.0, min(100.0, score))

    erro = bool(
        baseline.p10
        and oferta.preco < baseline.p10 * float(cfg["alertas"]["fator_erro_tarifa"])
        and baseline.n >= int(cfg["historico"]["minimo_observacoes"])
    )

    return Avaliacao(
        score=round(score, 1), desconto=desconto, baseline=baseline,
        partes={k: round(v, 1) for k, v in partes.items()}, erro_de_tarifa=erro,
    )


def _pontos_horario(oferta: Oferta, prefs: dict) -> float:
    h_partida = oferta.hora_partida()
    h_chegada = oferta.hora_chegada()
    if h_partida is None and h_chegada is None:
        return 0.6                          # sem informacao: nota neutra

    limite_partida = int(str(prefs["hora_partida_min"]).split(":")[0])
    limite_chegada = int(str(prefs["hora_chegada_max"]).split(":")[0])

    ok_partida = h_partida is None or (limite_partida <= h_partida <= 22)
    ok_chegada = h_chegada is None or (6 <= h_chegada <= limite_chegada)

    if ok_partida and ok_chegada:
        return 1.0
    if ok_partida or ok_chegada:
        return 0.625
    return 0.25


# ------------------------------------------------------------------
def custo_total(oferta: Oferta, cfg: Config) -> float:
    """Preco da passagem + custo de chegar naquele aeroporto."""
    return oferta.preco + cfg.custo_deslocamento(oferta.origem)
