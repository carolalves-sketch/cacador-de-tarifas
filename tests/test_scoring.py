"""Testes do score e das travas de alerta.

Rode com:  python3 -m pytest tests -q
(ou simplesmente:  python3 tests/test_scoring.py)
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.db import Oferta, agora_br
from src.scoring import Baseline, interpolar, pontuar

CFG = Config.carregar()


def _oferta(**kw) -> Oferta:
    padrao = dict(
        coletado_em=agora_br().isoformat(timespec="seconds"),
        fonte="google_flights", origem="BSB", destino="AMS",
        data_partida="2027-04-15", preco=2000.0,
        companhia="KLM", companhias="KLM", escalas=0,
        duracao_min=720, partida_iso="2027-04-15T22:10",
        chegada_iso="2027-04-16T13:15", bagagem="despachada",
    )
    padrao.update(kw)
    return Oferta(**padrao)


def _base(valor=2500.0, n=30, confianca="alta") -> Baseline:
    return Baseline(valor=valor, p10=valor * 0.8, n=n, confianca=confianca,
                    origem_do_calculo="teste", media=valor, minimo=valor * 0.7)


def test_curva_de_preco_satura():
    assert interpolar(-0.1) == 0.0
    assert interpolar(0.0) == 0.0
    assert 0.30 < interpolar(0.10) < 0.36
    assert interpolar(0.50) == 1.0
    assert interpolar(0.90) == 1.0, "descontos absurdos nao podem valer mais que 100%"


def test_curva_e_monotonica():
    anteriores = [interpolar(x / 100) for x in range(0, 60)]
    assert all(b >= a for a, b in zip(anteriores, anteriores[1:]))


def test_voo_perfeito_com_preco_normal_nao_alcanca_o_limite():
    """O ponto central do desenho: qualidade sozinha nao vira alerta."""
    av = pontuar(_oferta(preco=2500.0), _base(2500.0), CFG, 720)
    assert av.score <= 45, f"score {av.score} alto demais para um preco normal"
    assert av.classificacao == "NORMAL"


def test_desconto_grande_com_itinerario_medio_passa_do_limite():
    av = pontuar(
        _oferta(preco=1700.0, escalas=1, conexao_min_minutos=120, bagagem="mao"),
        _base(2500.0), CFG, 720,
    )
    assert av.score >= 75
    assert av.classificacao in ("BOA", "EXCELENTE")


def test_mais_escalas_reduz_o_score():
    direto = pontuar(_oferta(preco=1800.0, escalas=0), _base(), CFG, 720).score
    uma = pontuar(_oferta(preco=1800.0, escalas=1, conexao_min_minutos=120), _base(), CFG, 720).score
    duas = pontuar(_oferta(preco=1800.0, escalas=2, conexao_min_minutos=120), _base(), CFG, 720).score
    assert direto > uma > duas


def test_conexao_curta_demais_penaliza():
    boa = pontuar(_oferta(preco=1800.0, escalas=1, conexao_min_minutos=120), _base(), CFG, 720)
    ruim = pontuar(_oferta(preco=1800.0, escalas=1, conexao_min_minutos=45), _base(), CFG, 720)
    assert ruim.score < boa.score
    assert ruim.partes.get("penalidade_conexao") == -3.0


def test_madrugada_vale_menos_que_horario_civilizado():
    dia = pontuar(_oferta(partida_iso="2027-04-15T10:00",
                          chegada_iso="2027-04-16T09:00"), _base(), CFG, 720)
    noite = pontuar(_oferta(partida_iso="2027-04-15T03:00",
                            chegada_iso="2027-04-16T04:00"), _base(), CFG, 720)
    assert noite.score < dia.score


def test_score_fica_entre_0_e_100():
    caro = pontuar(_oferta(preco=9000.0, escalas=3), _base(2000.0), CFG, 720)
    barato = pontuar(_oferta(preco=200.0), _base(4000.0), CFG, 720)
    assert 0 <= caro.score <= 100
    assert 0 <= barato.score <= 100


def test_detector_de_erro_de_tarifa():
    normal = pontuar(_oferta(preco=1900.0), _base(2500.0), CFG, 720)
    absurdo = pontuar(_oferta(preco=900.0), _base(2500.0), CFG, 720)
    assert not normal.erro_de_tarifa
    assert absurdo.erro_de_tarifa, "preco muito abaixo do p10 deveria ser sinalizado"


def test_baseline_sem_historico_nao_dispara_erro_de_tarifa():
    """Com pouca observacao, o detector precisa ficar quieto."""
    av = pontuar(_oferta(preco=500.0), _base(2500.0, n=0, confianca="baixa"), CFG, 720)
    assert not av.erro_de_tarifa


def test_pesos_somam_cem():
    pesos = CFG["score"]
    assert sum(v for k, v in pesos.items() if k.startswith("peso_")) == 100


def test_janela_de_datas_esta_correta():
    datas = CFG.datas_janela()
    assert datas[0] == date(2027, 4, 13)
    assert datas[-1] == date(2027, 4, 23)
    assert len(datas) == 11
    assert len(CFG.codigos_origem) * len(CFG.codigos_destino) * len(datas) == 198


def test_custo_de_deslocamento_muda_o_ranking():
    from src.scoring import custo_total
    bsb = _oferta(origem="BSB", preco=2480.0)
    gig = _oferta(origem="GIG", preco=2150.0)
    assert gig.preco < bsb.preco
    assert custo_total(gig, CFG) > custo_total(bsb, CFG), (
        "Rio parece mais barato, mas com o traslado deveria ficar acima de Brasilia"
    )


if __name__ == "__main__":
    falhas = 0
    for nome, func in sorted(globals().items()):
        if nome.startswith("test_") and callable(func):
            try:
                func()
                print(f"  ok   {nome}")
            except AssertionError as e:
                falhas += 1
                print(f"  FALHOU {nome}: {e}")
    print(f"\n{falhas} falha(s).")
    raise SystemExit(1 if falhas else 0)
