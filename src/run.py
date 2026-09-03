"""Orquestrador. E este arquivo que o GitHub Actions executa.

Modos:
  varredura    camada gratuita (Travelpayouts) - varre tudo, nao alerta
  grade        camada paga (Google Flights) - confirma N datas em rodizio
  zoom         confirma o melhor candidato apontado pela varredura
  comparativo  1 busca semanal de ida e volta, so para referencia
  painel       apenas regenera o dashboard
  simular      gera dados ficticios para testar sem gastar cota
  auto         decide pelo horario (usado pelo agendamento)
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import traceback
from datetime import date, datetime, timedelta

from .alerts import Telegram, deve_alertar, montar_mensagem
from .config import Config, PASTA_LOGS
from .dashboard import duracao_mediana, gerar as gerar_painel
from .db import Estado, Historico, Oferta, agora_br
from .scoring import calcular_baseline, pontuar
from .sources.serpapi_flights import ErroFonte, GoogleFlights
from .sources.travelpayouts import Travelpayouts

log = logging.getLogger("cacador")


# ------------------------------------------------------------------
def configurar_log(verboso: bool = False) -> None:
    PASTA_LOGS.mkdir(parents=True, exist_ok=True)
    arquivo = PASTA_LOGS / f"{agora_br():%Y-%m}.log"
    formato = "%(asctime)s  %(levelname)-7s %(name)s  %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if verboso else logging.INFO,
        format=formato,
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(arquivo, encoding="utf-8")],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ------------------------------------------------------------------
class Cota:
    """Trava de seguranca: nunca deixa passar do limite gratuito."""

    def __init__(self, cfg: Config, estado: Estado) -> None:
        self.cfg, self.estado = cfg, estado
        self.limite = int(cfg["cota"]["parar_em"])

    @property
    def usado(self) -> int:
        return int(self.estado["consumo_cota"] or 0)

    def disponivel(self, quantas: int = 1) -> bool:
        return self.usado + quantas <= self.limite

    def gastar(self, quantas: int = 1) -> None:
        self.estado["consumo_cota"] = self.usado + quantas
        log.info("Cota: %s/%s buscas usadas neste mes.", self.usado, self.cfg["cota"]["limite_mensal"])


# ------------------------------------------------------------------
def datas_da_rodada(cfg: Config, estado: Estado, quantas: int) -> list[date]:
    """Rodizio: cada rodada pega o proximo pedaco da janela."""
    todas = cfg.datas_janela()
    if not todas:
        return []
    inicio = int(estado["indice_grade"] or 0) % len(todas)
    escolhidas = [todas[(inicio + i) % len(todas)] for i in range(min(quantas, len(todas)))]
    estado["indice_grade"] = (inicio + len(escolhidas)) % len(todas)
    return escolhidas


# ------------------------------------------------------------------
def rodar_varredura(cfg: Config, hist: Historico) -> int:
    """Camada gratuita. Nunca gera alerta - so historico e candidatos."""
    fonte = Travelpayouts(cfg.chaves.travelpayouts)
    meses = sorted({d.strftime("%Y-%m") for d in cfg.datas_com_margem()})
    permitidas = {d.isoformat() for d in cfg.datas_com_margem()}
    total = 0
    falhas = 0

    for origem in cfg.codigos_origem:
        for destino in cfg.codigos_destino:
            encontradas: list[Oferta] = []
            for mes in meses:
                try:
                    encontradas += fonte.buscar_rota(origem, destino, mes, cfg.somente_ida)
                except ErroFonte as e:
                    falhas += 1
                    log.warning("Varredura %s-%s (%s): %s", origem, destino, mes, e)
            uteis = [o for o in encontradas if o.data_partida in permitidas]
            total += hist.gravar_ofertas(uteis)
            log.info("Varredura %s-%s: %s ofertas na janela.", origem, destino, len(uteis))

    if falhas and total == 0:
        raise ErroFonte("A varredura gratuita falhou em todas as rotas.")
    log.info("Varredura concluida: %s ofertas gravadas.", total)
    return total


# ------------------------------------------------------------------
def rodar_grade(cfg: Config, hist: Historico, estado: Estado, quantas: int) -> list[Oferta]:
    """Camada paga: confirma preco real de algumas datas."""
    cota = Cota(cfg, estado)
    if not cota.disponivel(quantas):
        restante = max(0, cota.limite - cota.usado)
        log.warning("Cota quase no fim (%s/%s). Reduzindo a rodada.", cota.usado, cota.limite)
        quantas = restante
    if quantas <= 0:
        log.warning("Cota do mes esgotada. Nenhuma busca paga nesta rodada.")
        return []

    fonte = GoogleFlights(cfg.chaves.serpapi)
    novas: list[Oferta] = []
    for dia in datas_da_rodada(cfg, estado, quantas):
        try:
            ofertas, insights = fonte.buscar(
                cfg.codigos_origem, cfg.codigos_destino, dia,
                adultos=int(cfg["viagem"]["passageiros"]), classe=cfg.classe_google,
            )
        except ErroFonte as e:
            log.error("Grade %s: %s", dia, e)
            continue
        cota.gastar(1)
        hist.gravar_ofertas(ofertas)
        novas += ofertas
        faixa = (insights or {}).get("typical_price_range")
        log.info("Grade %s: %s ofertas · faixa tipica do Google %s", dia, len(ofertas), faixa)
    return novas


# ------------------------------------------------------------------
def rodar_zoom(cfg: Config, hist: Historico, estado: Estado) -> list[Oferta]:
    """Confirma a celula mais fora do padrao apontada pela camada gratuita."""
    cota = Cota(cfg, estado)
    if not cota.disponivel(1):
        log.warning("Sem cota para o zoom.")
        return []

    candidatos = []
    janela = int(cfg["historico"]["janela_dias"])
    for c in hist.melhor_por_celula(janela_dias=1):
        preco = float(c["preco"])
        base = hist.estatisticas(hist.precos(c["origem"], c["destino"], janela_dias=janela))
        if base["n"] < int(cfg["historico"]["minimo_observacoes"]) or not base.get("mediana"):
            continue
        desconto = 1 - preco / base["mediana"]
        if desconto > 0.10:
            candidatos.append((desconto, c["data_partida"]))
    if not candidatos:
        log.info("Nenhum candidato fora do padrao para o zoom.")
        return []

    candidatos.sort(reverse=True)
    dia = date.fromisoformat(candidatos[0][1])
    log.info("Zoom em %s (desconto aparente de %.0f%%).", dia, candidatos[0][0] * 100)

    fonte = GoogleFlights(cfg.chaves.serpapi)
    try:
        ofertas, _ = fonte.buscar(
            cfg.codigos_origem, cfg.codigos_destino, dia,
            adultos=int(cfg["viagem"]["passageiros"]), classe=cfg.classe_google,
        )
    except ErroFonte as e:
        log.error("Zoom falhou: %s", e)
        return []
    cota.gastar(1)
    hist.gravar_ofertas(ofertas)
    return ofertas


# ------------------------------------------------------------------
def rodar_comparativo(cfg: Config, hist: Historico, estado: Estado) -> None:
    """1 busca semanal de ida e volta, so para referencia no painel."""
    if not cfg["comparativo_ida_volta"]["ativo"] or not cfg.somente_ida:
        return
    hoje = agora_br().date()
    if estado["ultimo_comparativo"] == hoje.isoformat():
        return
    cota = Cota(cfg, estado)
    if not cota.disponivel(1):
        return

    ida = cfg.partida_de + timedelta(days=(cfg.partida_ate - cfg.partida_de).days // 2)
    volta = ida + timedelta(days=int(cfg["viagem"]["duracao_min"]))
    fonte = GoogleFlights(cfg.chaves.serpapi)
    try:
        ofertas, _ = fonte.buscar(
            cfg.codigos_origem, cfg.codigos_destino, ida, volta,
            adultos=int(cfg["viagem"]["passageiros"]), classe=cfg.classe_google,
        )
    except ErroFonte as e:
        log.error("Comparativo ida e volta falhou: %s", e)
        return
    cota.gastar(1)
    hist.gravar_ofertas(ofertas)
    estado["ultimo_comparativo"] = hoje.isoformat()
    if ofertas:
        log.info("Comparativo ida e volta (%s → %s): a partir de R$ %.0f",
                 ida, volta, min(o.preco for o in ofertas))


# ------------------------------------------------------------------
def avaliar_e_alertar(cfg: Config, hist: Historico, ofertas: list[Oferta]) -> int:
    """Pontua as ofertas novas e envia os alertas que passarem nas travas."""
    if not ofertas:
        return 0
    telegram = Telegram(cfg.chaves.telegram_token, cfg.chaves.telegram_chat_id)

    avaliadas = []
    for oferta in ofertas:
        if oferta.data_volta:          # comparativo nao gera alerta
            continue
        base = calcular_baseline(hist, cfg, oferta)
        dur = duracao_mediana(hist, oferta.origem, oferta.destino)
        avaliadas.append((oferta, pontuar(oferta, base, cfg, dur)))

    avaliadas.sort(key=lambda x: x[1].score, reverse=True)
    enviados = 0
    for oferta, av in avaliadas:
        alertar, motivo = deve_alertar(hist, cfg, oferta, av)
        if not alertar:
            log.debug("Sem alerta %s %s R$%.0f score %.0f - %s",
                      oferta.rota, oferta.data_partida, oferta.preco, av.score, motivo)
            continue
        texto = montar_mensagem(cfg, hist, oferta, av, motivo)
        if telegram.enviar(texto):
            hist.gravar_alerta(oferta, av.score, motivo)
            enviados += 1
            log.info("ALERTA enviado: %s %s R$%.0f score %.0f (%s)",
                     oferta.rota, oferta.data_partida, oferta.preco, av.score, motivo)
        else:
            log.error("Alerta nao pode ser entregue - nao sera marcado como enviado.")
    return enviados


# ------------------------------------------------------------------
def avisar_falha(cfg: Config, estado: Estado, mensagem: str) -> None:
    """Avisa no Telegram quando o sistema quebra - no maximo 1 vez por dia."""
    hoje = agora_br().date().isoformat()
    if estado["ultimo_aviso_falha"] == hoje:
        return
    telegram = Telegram(cfg.chaves.telegram_token, cfg.chaves.telegram_chat_id)
    if telegram.enviar(
        "⚠️ <b>Caçador de Tarifas — falha na rodada</b>\n\n"
        + mensagem[:800]
        + "\n\nO histórico e o painel continuam intactos. "
          "Se isso se repetir amanhã, vale conferir as chaves nos Secrets do GitHub."
    ):
        estado["ultimo_aviso_falha"] = hoje


# ------------------------------------------------------------------
def simular(cfg: Config, hist: Historico, dias: int = 21) -> None:
    """Gera historico ficticio para testar o sistema sem gastar cota."""
    random.seed(42)
    cias = ["TAP Air Portugal", "LATAM", "KLM", "Air France", "Lufthansa", "Iberia"]
    base_rota = {}
    for o in cfg.codigos_origem:
        for d in cfg.codigos_destino:
            base_rota[(o, d)] = random.uniform(1600, 2600) + (0 if o == "GRU" else 250)

    ofertas = []
    for atras in range(dias, -1, -1):
        momento = agora_br() - timedelta(days=atras)
        for o in cfg.codigos_origem:
            for d in cfg.codigos_destino:
                for dia in cfg.datas_janela()[::2]:
                    preco = base_rota[(o, d)] * random.uniform(0.88, 1.18)
                    if atras == 0 and random.random() < 0.03:
                        preco *= 0.62                     # uma promocao de verdade
                    escalas = random.choice([0, 1, 1, 2])
                    partida = momento.replace(hour=random.choice([8, 14, 19, 22]), minute=10)
                    ofertas.append(Oferta(
                        coletado_em=momento.isoformat(timespec="seconds"),
                        fonte="google_flights", origem=o, destino=d,
                        data_partida=dia.isoformat(), preco=round(preco, 2),
                        companhia=random.choice(cias),
                        companhias=random.choice(cias),
                        numero_voo=f"XX{random.randint(100, 999)}",
                        escalas=escalas,
                        conexoes="LIS 1h40" if escalas else "",
                        conexao_min_minutos=100 if escalas else 0,
                        duracao_min=random.randint(660, 1080),
                        partida_iso=f"{dia.isoformat()}T{partida.hour:02d}:10",
                        chegada_iso=f"{(dia + timedelta(days=1)).isoformat()}T{(partida.hour + 2) % 24:02d}:35",
                        bagagem=random.choice(["despachada", "mao", "desconhecida"]),
                        link="https://www.google.com/travel/flights",
                    ))
    hist.gravar_ofertas(ofertas)
    log.info("Simulacao: %s ofertas ficticias gravadas.", len(ofertas))


# ------------------------------------------------------------------
def modo_por_horario(cfg: Config) -> str:
    hora = agora_br().hour
    if hora in (5, 6, 7, 12, 13, 14, 19, 20):
        return "varredura"
    return "grade"


def principal(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cacador de tarifas BR-Europa")
    ap.add_argument("--modo", default="auto",
                    choices=["auto", "varredura", "grade", "zoom", "comparativo",
                             "painel", "simular", "tudo"])
    ap.add_argument("--datas", type=int, default=None,
                    help="quantas datas confirmar no modo grade")
    ap.add_argument("--sem-alerta", action="store_true", help="nao envia nada no Telegram")
    ap.add_argument("-v", "--verboso", action="store_true")
    args = ap.parse_args(argv)

    configurar_log(args.verboso)
    cfg = Config.carregar()
    estado = Estado()
    hist = Historico()

    faltando = cfg.chaves.faltando()
    modo = modo_por_horario(cfg) if args.modo == "auto" else args.modo
    if modo not in ("painel", "simular") and faltando:
        log.error("Faltam segredos: %s. Configure em Settings > Secrets do repositorio.",
                  ", ".join(faltando))
        return 2

    log.info("=== Rodada '%s' em %s ===", modo, agora_br().strftime("%d/%m/%Y %H:%M"))
    novas: list[Oferta] = []
    try:
        if modo == "varredura":
            rodar_varredura(cfg, hist)
        elif modo == "grade":
            quantas = args.datas or int(cfg["frequencia"]["datas_por_rodada_grade"])
            novas = rodar_grade(cfg, hist, estado, quantas)
            rodar_comparativo(cfg, hist, estado)
        elif modo == "zoom":
            novas = rodar_zoom(cfg, hist, estado)
        elif modo == "comparativo":
            rodar_comparativo(cfg, hist, estado)
        elif modo == "simular":
            simular(cfg, hist)
        elif modo == "tudo":
            rodar_varredura(cfg, hist)
            quantas = args.datas or int(cfg["frequencia"]["datas_por_rodada_grade"])
            novas = rodar_grade(cfg, hist, estado, quantas)
            novas += rodar_zoom(cfg, hist, estado)
    except ErroFonte as e:
        log.error("Rodada interrompida: %s", e)
        avisar_falha(cfg, estado, str(e))
    except Exception as e:                                  # noqa: BLE001
        log.error("Erro inesperado: %s\n%s", e, traceback.format_exc())
        avisar_falha(cfg, estado, f"{type(e).__name__}: {e}")

    if not args.sem_alerta and modo in ("grade", "zoom", "tudo"):
        enviados = avaliar_e_alertar(cfg, hist, novas)
        log.info("Alertas enviados nesta rodada: %s", enviados)

    try:
        gerar_painel(cfg, hist, estado)
    except Exception as e:                                  # noqa: BLE001
        log.error("Falha ao gerar o painel: %s", e)

    log.info("=== Fim da rodada. Historico com %s ofertas. ===", hist.total_ofertas())
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
