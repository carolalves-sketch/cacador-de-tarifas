"""Decide o que vira alerta, monta a mensagem e envia pelo Telegram."""
from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta

import requests

from .config import Config
from .db import Historico, Oferta, agora_br
from .scoring import Avaliacao, custo_total

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
#  Decisao
# ------------------------------------------------------------------
def deve_alertar(
    hist: Historico, cfg: Config, oferta: Oferta, av: Avaliacao
) -> tuple[bool, str]:
    """Devolve (alerta?, motivo). O motivo entra no historico e no painel."""
    a = cfg["alertas"]
    orc = cfg["orcamento"]

    if oferta.fonte != "google_flights":
        return False, "fonte nao confirmada"

    # ---- limites globais do dia ----
    if hist.alertas_de_hoje() >= int(a["maximo_por_dia"]) and not av.erro_de_tarifa:
        return False, "limite diario de alertas atingido"

    ultimo_rota = hist.ultimo_alerta_da_rota(oferta.origem, oferta.destino)
    if ultimo_rota and not av.erro_de_tarifa:
        try:
            quando = datetime.fromisoformat(ultimo_rota["enviado_em"])
            horas = (agora_br() - quando).total_seconds() / 3600
            if horas < float(a["intervalo_por_rota_horas"]):
                return False, "intervalo minimo da rota nao cumprido"
        except (TypeError, ValueError):
            pass

    # ---- caminhos que furam a fila ----
    if av.erro_de_tarifa:
        return _checar_repeticao(hist, cfg, oferta, av, "possivel erro de tarifa")
    if oferta.preco <= float(orc["chao_alerta"]):
        # O chao e um atalho para preco baixo, nao um passe livre para
        # itinerario ruim: ainda exige uma qualidade minima.
        if av.score >= float(a.get("score_minimo_chao", 60)):
            return _checar_repeticao(
                hist, cfg, oferta, av, "preco abaixo do chao de oportunidade"
            )
    if av.score >= 90:
        return _checar_repeticao(hist, cfg, oferta, av, "score excelente")

    # ---- travas normais ----
    if av.baseline.confianca == "baixa":
        score_min = float(a["score_minimo_confianca_baixa"])
        desc_min = float(a["desconto_minimo_confianca_baixa"])
    else:
        score_min = float(a["score_minimo"])
        desc_min = float(a["desconto_minimo"])

    if av.score < score_min:
        return False, f"score {av.score} abaixo de {score_min}"
    if av.desconto < desc_min:
        return False, f"desconto {av.desconto:.0%} abaixo de {desc_min:.0%}"
    if oferta.preco > float(orc["teto_absoluto"]) and av.desconto < float(orc["desconto_excecao"]):
        return False, "acima do teto sem desconto excepcional"
    if oferta.escalas > int(cfg["preferencias"]["max_escalas"]):
        return False, "escalas demais"
    if cfg["preferencias"]["bagagem_obrigatoria"] and oferta.bagagem not in ("despachada",):
        return False, "sem bagagem despachada"

    return _checar_repeticao(hist, cfg, oferta, av, "oportunidade dentro dos criterios")


def _checar_repeticao(
    hist: Historico, cfg: Config, oferta: Oferta, av: Avaliacao, motivo: str
) -> tuple[bool, str]:
    """Impede o mesmo alerta duas vezes; libera quando algo mudou de verdade."""
    a = cfg["alertas"]
    anterior = hist.ultimo_alerta(oferta.chave_alerta)
    if anterior is None:
        return True, motivo

    preco_antes = float(anterior["preco"] or 0)
    score_antes = float(anterior["score"] or 0)

    queda_reais = preco_antes - oferta.preco
    queda_pct = queda_reais / preco_antes if preco_antes else 0
    gatilho_reais = max(float(a["queda_minima_reais"]), preco_antes * float(a["queda_para_reenviar"]))

    if queda_reais >= gatilho_reais:
        return True, f"queda de R$ {queda_reais:,.0f} ({queda_pct:.0%}) desde o ultimo alerta"

    if av.score - score_antes >= float(a["subida_score_para_reenviar"]):
        return True, f"score subiu de {score_antes:.0f} para {av.score:.0f}"

    faixa_antes = _faixa(score_antes)
    if faixa_antes != av.classificacao and av.score > score_antes:
        return True, f"mudou de {faixa_antes} para {av.classificacao}"

    dias_lembrete = int(a.get("lembrete_dias") or 0)
    if dias_lembrete:
        try:
            quando = datetime.fromisoformat(anterior["enviado_em"])
            if (agora_br() - quando) >= timedelta(days=dias_lembrete):
                return True, f"lembrete: continua disponivel ha {dias_lembrete} dias"
        except (TypeError, ValueError):
            pass

    return False, "ja alertado e sem mudanca relevante"


def _faixa(score: float) -> str:
    if score >= 90:
        return "EXCELENTE"
    if score >= 75:
        return "BOA"
    if score >= 60:
        return "INTERESSANTE"
    return "NORMAL"


# ------------------------------------------------------------------
#  Mensagem
# ------------------------------------------------------------------
def montar_mensagem(
    cfg: Config,
    hist: Historico,
    oferta: Oferta,
    av: Avaliacao,
    motivo: str,
) -> str:
    o = oferta
    b = av.baseline
    economia = max(0.0, b.valor - o.preco)

    linhas: list[str] = []
    linhas.append(f"<b>{av.emoji} OPORTUNIDADE DE VOO — {av.classificacao}</b>")
    if cfg.somente_ida:
        linhas.append("<i>Somente ida</i>")
    if not cfg.dentro_da_janela(date.fromisoformat(o.data_partida)):
        linhas.append("<i>⚠ fora da janela de 13 a 23/04</i>")
    if av.erro_de_tarifa:
        linhas.append(
            "<i>⚠ preco muito abaixo do padrao — pode ser erro de tarifa. "
            "Costuma durar pouco e a companhia pode cancelar. "
            "Se comprar, evite emitir hoteis e traslados antes de confirmar.</i>"
        )
    linhas.append("")

    linhas.append(f"Origem:    {cfg.nome_aeroporto(o.origem)} ({o.origem})")
    linhas.append(f"Destino:   {cfg.nome_aeroporto(o.destino)} ({o.destino})")
    linhas.append(f"Partida:   {_data_br(o.data_partida)}{_hora(o.partida_iso)}")
    if o.chegada_iso:
        linhas.append(f"Chegada:   {_data_iso_br(o.chegada_iso)}{_hora(o.chegada_iso)}")
    if o.duracao_min:
        direto = " · voo direto" if o.escalas == 0 else ""
        linhas.append(f"Duracao:   {_dur(o.duracao_min)}{direto}")
    linhas.append("")

    marcador = ""
    referencia = float(cfg["orcamento"]["referencia"])
    if o.preco <= referencia:
        marcador = f"   ▼ abaixo do orcamento de {_rs(referencia)}"
    linhas.append(f"<b>Preco:     {_rs(o.preco)}</b>{marcador}")
    if b.media:
        linhas.append(f"Media:     {_rs(b.media)}")
    linhas.append(f"Mediana:   {_rs(b.valor)}   ({b.origem_do_calculo}, n={b.n})")
    if economia > 0:
        linhas.append(f"Economia:  {_rs(economia)} ({av.desconto:.0%} abaixo do normal)")
    # O menor historico e sempre o da rota inteira, nao o desta data -
    # senao a propria oferta vira o recorde e a linha nao informa nada.
    resumo = hist.resumo_rota(o.origem, o.destino)
    minimo_rota = resumo.get("minimo")
    if minimo_rota and minimo_rota < o.preco:
        linhas.append(f"Menor ja visto nesta rota: {_rs(minimo_rota)}")
    elif minimo_rota:
        linhas.append("<b>E o menor preco ja visto nesta rota.</b>")
    linhas.append("")

    linhas.append(f"Companhia: {o.companhia or '—'}")
    linhas.append(f"Escalas:   {'nenhuma' if o.escalas == 0 else f'{o.escalas} ({o.conexoes})'}")
    linhas.append(f"Bagagem:   {_bagagem(o.bagagem)}")
    linhas.append(f"Score:     {av.score:.0f}/100")

    comparacao = _comparar_origens(cfg, hist, o)
    if comparacao:
        linhas.append("")
        linhas.append("<b>── Comparacao de origens ──</b>")
        linhas.extend(comparacao)

    datas = _melhores_datas(cfg, hist, o)
    if datas:
        linhas.append("")
        linhas.append("<b>── Melhores datas da janela ──</b>")
        linhas.extend(datas)

    linhas.append("")
    linhas.append(f"<i>Motivo do alerta: {motivo}</i>")
    linhas.append(f"Fonte: Google Flights · coletado {agora_br():%d/%m as %Hh%M}")
    if o.link:
        linhas.append(f'<a href="{html.escape(o.link, quote=True)}">Abrir no Google Voos</a>')

    return "\n".join(linhas)


def _comparar_origens(cfg: Config, hist: Historico, oferta: Oferta) -> list[str]:
    celulas = [
        c for c in hist.melhor_por_celula(janela_dias=2)
        if c["destino"] == oferta.destino and c["data_partida"] == oferta.data_partida
    ]
    if len(celulas) < 2:
        return []

    itens = []
    for c in celulas:
        preco = float(c["preco"])
        itens.append({
            "origem": c["origem"],
            "preco": preco,
            "total": preco + cfg.custo_deslocamento(c["origem"]),
        })
    itens.sort(key=lambda i: i["total"])

    linhas = []
    for i, item in enumerate(itens):
        extra = "  ← melhor" if i == 0 else ""
        if cfg.custo_deslocamento(item["origem"]):
            linhas.append(
                f"{item['origem']}  {_rs(item['preco'])} · total {_rs(item['total'])}{extra}"
            )
        else:
            linhas.append(f"{item['origem']}  {_rs(item['preco'])} · total {_rs(item['total'])}{extra}")

    principal = next((i for i in itens if i["origem"] == "BSB"), None)
    melhor = itens[0]
    if principal and melhor["origem"] != "BSB":
        dif_bruta = principal["preco"] - melhor["preco"]
        dif_real = principal["total"] - melhor["total"]
        linhas.append("")
        linhas.append(
            f"{cfg.nome_aeroporto(melhor['origem'])} esta {_rs(dif_bruta)} "
            f"mais barato que Brasilia."
        )
        linhas.append(f"Descontado o deslocamento, a economia real e {_rs(dif_real)}.")
    return linhas


def _melhores_datas(cfg: Config, hist: Historico, oferta: Oferta, quantas: int = 3) -> list[str]:
    celulas = [
        c for c in hist.melhor_por_celula(janela_dias=2)
        if c["origem"] == oferta.origem and c["destino"] == oferta.destino
    ]
    if len(celulas) < 2:
        return []
    celulas.sort(key=lambda c: float(c["preco"]))
    linhas = []
    for c in celulas[:quantas]:
        marca = "   ← esta" if c["data_partida"] == oferta.data_partida else ""
        linhas.append(f"{_data_br(c['data_partida'])}  {_rs(float(c['preco']))}{marca}")
    return linhas


# ---------- formatadores ----------
def _rs(valor: float) -> str:
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


def _data_br(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d/%m/%Y")
    except ValueError:
        return iso


def _data_iso_br(iso: str) -> str:
    return _data_br(iso.split("T")[0])


def _hora(iso: str) -> str:
    if not iso or "T" not in iso:
        return ""
    return " · " + iso.split("T")[1][:5].replace(":", "h")


def _dur(minutos: int) -> str:
    return f"{minutos // 60}h{minutos % 60:02d}"


def _bagagem(valor: str) -> str:
    return {
        "despachada": "despachada incluida",
        "mao": "somente de mao",
        "nenhuma": "tarifa basica, sem bagagem",
        "desconhecida": "nao informada",
    }.get(valor, valor)


# ------------------------------------------------------------------
#  Envio
# ------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    @property
    def configurado(self) -> bool:
        return bool(self.token and self.chat_id)

    def enviar(self, texto: str) -> bool:
        if not self.configurado:
            log.warning("Telegram nao configurado - alerta nao enviado.")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": texto,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
        except requests.RequestException as e:
            log.error("Falha de rede ao enviar alerta: %s", e)
            return False
        if resp.status_code >= 400:
            log.error("Telegram recusou a mensagem (%s): %s", resp.status_code, resp.text[:300])
            return False
        return True
