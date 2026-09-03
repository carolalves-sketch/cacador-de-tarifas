"""Gera docs/index.html - o painel publicado pelo GitHub Pages.

Pagina unica, autocontida, sem bibliotecas externas. Os dados sao embutidos
como JSON e desenhados com SVG e HTML puros.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config, PASTA_PAINEL
from .db import Historico, agora_br
from .scoring import Baseline, calcular_baseline, custo_total, pontuar

log = logging.getLogger(__name__)


def gerar(cfg: Config, hist: Historico, estado, saida: Path | None = None) -> Path:
    saida = saida or (PASTA_PAINEL / "index.html")
    saida.parent.mkdir(parents=True, exist_ok=True)
    dados = montar_dados(cfg, hist, estado)
    html = MODELO.replace("__DADOS__", json.dumps(dados, ensure_ascii=False))
    saida.write_text(html, encoding="utf-8")
    log.info("Painel gerado em %s", saida)
    return saida


# ------------------------------------------------------------------
def montar_dados(cfg: Config, hist: Historico, estado) -> dict:
    datas = [d.isoformat() for d in cfg.datas_janela()]
    rotas = [(o, d) for o in cfg.codigos_origem for d in cfg.codigos_destino]

    celulas_atuais = {
        (c["origem"], c["destino"], c["data_partida"]): float(c["preco"])
        for c in hist.melhor_por_celula(janela_dias=3)
    }

    # baseline por rota, para colorir o mapa de calor
    base_rota: dict[str, dict] = {}
    for o, d in rotas:
        resumo = hist.resumo_rota(o, d, int(cfg["historico"]["janela_dias"]))
        base_rota[f"{o}-{d}"] = {
            "mediana": resumo.get("mediana"),
            "minimo": resumo.get("minimo"),
            "media": resumo.get("media"),
            "n": resumo.get("n", 0),
            "ultimos_7": resumo.get("ultimos_7"),
            "variacao": resumo.get("variacao_pct"),
        }

    grade = []
    for o, d in rotas:
        chave = f"{o}-{d}"
        linha = {"rota": chave, "origem": o, "destino": d,
                 "base": base_rota[chave]["mediana"], "celulas": []}
        for dia in datas:
            preco = celulas_atuais.get((o, d, dia))
            linha["celulas"].append({
                "data": dia,
                "preco": preco,
                "total": (preco + cfg.custo_deslocamento(o)) if preco else None,
            })
        grade.append(linha)

    # --- destaques ---
    disponiveis = [
        {"origem": o, "destino": d, "data": dia, "preco": p,
         "total": p + cfg.custo_deslocamento(o)}
        for (o, d, dia), p in celulas_atuais.items()
        if dia in datas
    ]
    mais_barato = min(disponiveis, key=lambda x: x["preco"], default=None)
    melhor_total = min(disponiveis, key=lambda x: x["total"], default=None)

    por_origem: dict[str, list[float]] = {}
    por_destino: dict[str, list[float]] = {}
    por_data: dict[str, list[float]] = {}
    for item in disponiveis:
        por_origem.setdefault(item["origem"], []).append(item["preco"])
        por_destino.setdefault(item["destino"], []).append(item["preco"])
        por_data.setdefault(item["data"], []).append(item["preco"])

    melhor_origem = min(
        ((k, min(v)) for k, v in por_origem.items()), key=lambda x: x[1], default=None
    )
    melhor_destino = min(
        ((k, min(v)) for k, v in por_destino.items()), key=lambda x: x[1], default=None
    )
    melhor_data = min(
        ((k, min(v)) for k, v in por_data.items()), key=lambda x: x[1], default=None
    )

    queda = maior_queda(hist, rotas, datas)

    # --- series historicas (uma rota por vez no grafico) ---
    series = {}
    for o, d in rotas:
        pontos = hist.serie_historica(o, d)
        if pontos:
            series[f"{o}-{d}"] = [{"dia": dia, "preco": p} for dia, p in pontos]

    # --- ofertas recentes com score ---
    ofertas = ofertas_recentes(cfg, hist)

    return {
        "gerado_em": agora_br().strftime("%d/%m/%Y às %Hh%M"),
        "janela": {"de": datas[0], "ate": datas[-1], "somente_ida": cfg.somente_ida},
        "datas": datas,
        "grade": grade,
        "base_rota": base_rota,
        "series": series,
        "ofertas": ofertas,
        "alertas": hist.alertas_recentes(40),
        "destaques": {
            "mais_barato": mais_barato,
            "melhor_total": melhor_total,
            "melhor_origem": melhor_origem,
            "melhor_destino": melhor_destino,
            "melhor_data": melhor_data,
            "queda": queda,
        },
        "cota": {
            "usado": estado["consumo_cota"],
            "limite": cfg["cota"]["limite_mensal"],
            "parar_em": cfg["cota"]["parar_em"],
            "mes": estado["mes_cota"],
        },
        "totais": {
            "ofertas": hist.total_ofertas(),
            "orcamento": cfg["orcamento"]["referencia"],
        },
        "nomes": {
            **{o["codigo"]: o["nome"] for o in cfg.origens},
            **{d["codigo"]: d["nome"] for d in cfg.destinos},
        },
        "deslocamento": {o["codigo"]: o.get("custo_deslocamento", 0) for o in cfg.origens},
    }


def maior_queda(hist: Historico, rotas, datas) -> dict | None:
    """Maior queda de preco nas ultimas 48 horas, por rota."""
    melhor = None
    limite_antigo = (agora_br() - timedelta(days=2)).isoformat()
    for o, d in rotas:
        antes = hist.con.execute(
            """SELECT MIN(preco) p FROM ofertas
               WHERE origem=? AND destino=? AND preco>0
                 AND coletado_em < ? AND coletado_em >= ?""",
            [o, d, limite_antigo, (agora_br() - timedelta(days=9)).isoformat()],
        ).fetchone()["p"]
        depois = hist.con.execute(
            """SELECT MIN(preco) p FROM ofertas
               WHERE origem=? AND destino=? AND preco>0 AND coletado_em >= ?""",
            [o, d, limite_antigo],
        ).fetchone()["p"]
        if antes and depois and depois < antes:
            delta = antes - depois
            if melhor is None or delta > melhor["delta"]:
                melhor = {"rota": f"{o}-{d}", "de": antes, "para": depois,
                          "delta": delta, "pct": delta / antes}
    return melhor


def ofertas_recentes(cfg: Config, hist: Historico, limite: int = 60) -> list[dict]:
    linhas = hist.con.execute(
        """SELECT * FROM ofertas WHERE fonte='google_flights'
           ORDER BY coletado_em DESC LIMIT ?""",
        [limite],
    ).fetchall()
    resultado = []
    for r in linhas:
        from .db import Oferta
        oferta = Oferta(**{k: r[k] for k in r.keys()})
        base = calcular_baseline(hist, cfg, oferta)
        dur = duracao_mediana(hist, oferta.origem, oferta.destino)
        av = pontuar(oferta, base, cfg, dur)
        resultado.append({
            "coletado_em": oferta.coletado_em,
            "origem": oferta.origem,
            "destino": oferta.destino,
            "data": oferta.data_partida,
            "preco": oferta.preco,
            "total": custo_total(oferta, cfg),
            "companhia": oferta.companhia,
            "escalas": oferta.escalas,
            "duracao": oferta.duracao_min,
            "bagagem": oferta.bagagem,
            "score": av.score,
            "classe": av.classificacao,
            "desconto": round(av.desconto, 3),
            "link": oferta.link,
        })
    return resultado


def duracao_mediana(hist: Historico, origem: str, destino: str) -> float | None:
    linha = hist.con.execute(
        """SELECT duracao_min FROM ofertas
           WHERE origem=? AND destino=? AND duracao_min>0
           ORDER BY duracao_min""",
        [origem, destino],
    ).fetchall()
    if not linha:
        return None
    valores = [r["duracao_min"] for r in linha]
    return valores[len(valores) // 2]


# ------------------------------------------------------------------
MODELO = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Caçador de Tarifas · Painel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400&display=swap">
<style>
:root{
  --ground:#F1F4F3; --surface:#FFFFFF; --surface-2:#E8EDEB;
  --ink:#141C20; --muted:#5C6B6E; --line:#D6DEDB; --line-strong:#B9C5C1;
  --accent:#0B6B5B; --accent-soft:#DCEDE8;
  --st-hot:#B03027; --st-good:#0B6B5B; --st-mid:#A07800; --st-normal:#5C6B6E;
  --dv--3:#0B6B5B; --dv--2:#4C9E8C; --dv--1:#A9CFC5; --dv-0:#E4E8E6;
  --dv-1:#E8BFB8;  --dv-2:#C97668;  --dv-3:#B03027;
  --dvi--3:#FFFFFF; --dvi--2:#0E2B25; --dvi--1:#132A25; --dvi-0:#141C20;
  --dvi-1:#3A1512;  --dvi-2:#2A0D0A;  --dvi-3:#FFFFFF;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --ground:#0D1416; --surface:#141E20; --surface-2:#1B2729;
  --ink:#E4EDEA; --muted:#94A5A3; --line:#243334; --line-strong:#334747;
  --accent:#48C3A6; --accent-soft:#12312C;
  --st-hot:#E8776B; --st-good:#48C3A6; --st-mid:#D9A93A; --st-normal:#94A3A3;
  --dv--3:#6FE0C2; --dv--2:#3FB395; --dv--1:#24685A; --dv-0:#2A3436;
  --dv-1:#6E3B33;  --dv-2:#B0554A;  --dv-3:#F09080;
  --dvi--3:#08201B; --dvi--2:#07211C; --dvi--1:#DDEFE9; --dvi-0:#E4EDEA;
  --dvi-1:#F4DCD8;  --dvi-2:#FFF0EC;  --dvi-3:#2A0D0A;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font-family:"Source Serif 4",Georgia,serif;font-size:16px;line-height:1.55;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
h1{font-family:"Archivo",system-ui,sans-serif;font-weight:700;font-size:30px;
  letter-spacing:-.02em;margin:0 0 6px}
h2{font-family:"Archivo",system-ui,sans-serif;font-weight:600;font-size:19px;
  margin:44px 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14.5px;margin:0 0 16px}
.mono{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
header .meta{display:flex;flex-wrap:wrap;gap:10px;align-items:center;
  font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted);
  letter-spacing:.05em;margin-top:10px}
.chip{background:var(--surface);border:1px solid var(--line);padding:4px 9px;border-radius:3px}
.chip.on{border-color:var(--accent);color:var(--accent)}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:22px 0 8px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:15px 17px}
.tile .k{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.11em;
  text-transform:uppercase;color:var(--muted);display:block;margin-bottom:8px}
.tile .v{font-family:"Archivo",system-ui,sans-serif;font-weight:700;font-size:26px;
  line-height:1.08;letter-spacing:-.02em;display:block;font-variant-numeric:tabular-nums}
.tile .n{font-size:13.5px;color:var(--muted);display:block;margin-top:6px;line-height:1.4}
.tile.hl{border-color:var(--accent);background:var(--accent-soft)}

.panel{background:var(--surface);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:8px 11px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--line)}
thead th{font-family:"Archivo",sans-serif;font-weight:600;font-size:11px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted);background:var(--surface-2);position:sticky;top:0}
tbody tr:last-child td{border-bottom:none}
td.n{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;text-align:right}

/* mapa de calor */
.heat td.cell{text-align:center;font-family:"IBM Plex Mono",monospace;font-size:12px;
  padding:0;border-bottom:2px solid var(--surface);border-right:2px solid var(--surface)}
.heat td.cell span{display:block;padding:7px 6px;border-radius:2px;cursor:default}
.heat th.rota{font-family:"IBM Plex Mono",monospace;font-size:12px;font-weight:600;
  color:var(--ink);background:var(--surface);position:sticky;left:0;z-index:2;
  border-right:1px solid var(--line-strong);text-transform:none;letter-spacing:0}
.heat thead th{font-size:11px}
.legend{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:10px 0 0;
  font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--muted)}
.legend i{width:22px;height:11px;display:inline-block;border-radius:2px}

.pill{display:inline-flex;gap:5px;align-items:center;font-family:"Archivo",sans-serif;
  font-size:10.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
  padding:2px 7px;border-radius:3px;border:1px solid currentColor}
.pill.EXCELENTE{color:var(--st-hot)} .pill.BOA{color:var(--st-good)}
.pill.INTERESSANTE{color:var(--st-mid)} .pill.NORMAL{color:var(--st-normal)}

.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
select,input[type=search]{font:inherit;font-size:14px;padding:6px 10px;border-radius:4px;
  border:1px solid var(--line-strong);background:var(--surface);color:var(--ink)}
select:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
a{color:var(--accent)}
.vazio{padding:26px;color:var(--muted);font-size:14.5px;text-align:center}
.tooltip{position:fixed;pointer-events:none;z-index:50;background:var(--ink);color:var(--ground);
  font-family:"IBM Plex Mono",monospace;font-size:11.5px;padding:7px 9px;border-radius:4px;
  line-height:1.5;opacity:0;transition:opacity .1s;max-width:260px}
.tooltip.on{opacity:1}
svg .grid{stroke:var(--line);stroke-width:1}
svg .serie{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .area{fill:var(--accent);opacity:.10}
svg .pt{fill:var(--accent);stroke:var(--surface);stroke-width:2}
svg .lbl{fill:var(--muted);font-family:"IBM Plex Mono",monospace;font-size:10.5px}
svg .cross{stroke:var(--line-strong);stroke-width:1;stroke-dasharray:3 3}
footer{margin-top:48px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--muted);font-size:13.5px}
@media(max-width:640px){ .wrap{padding:20px 14px 60px} h1{font-size:24px} }
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Caçador de Tarifas · BR&nbsp;→&nbsp;Europa</h1>
  <p class="sub" id="subtitulo"></p>
  <div class="meta" id="meta"></div>
</header>

<div class="tiles" id="tiles"></div>

<h2>Mapa de preços</h2>
<p class="sub">Cada linha é uma rota, cada coluna um dia de partida. A cor compara o preço com o normal <em>daquela rota</em> — não com as outras.</p>
<div class="panel scroll"><table class="heat" id="heat"></table></div>
<div class="legend" id="legenda"></div>

<h2>Histórico de preços</h2>
<p class="sub">Menor preço observado por dia. Escolha a rota.</p>
<div class="controls">
  <label>Rota <select id="selRota"></select></label>
</div>
<div class="panel" id="graficoBox"><svg id="grafico" width="100%" height="260" role="img" aria-label="Histórico do menor preço por dia"></svg></div>

<h2>Oportunidades recentes</h2>
<p class="sub">Últimas ofertas confirmadas com preço real, já pontuadas.</p>
<div class="controls">
  <label>Origem <select id="fOrigem"></select></label>
  <label>Destino <select id="fDestino"></select></label>
  <label>Score mínimo <select id="fScore">
    <option value="0">todas</option><option value="60">60+</option>
    <option value="75">75+</option><option value="90">90+</option></select></label>
</div>
<div class="panel scroll"><table id="tabela"></table></div>

<h2>Alertas enviados</h2>
<div class="panel scroll"><table id="tabAlertas"></table></div>

<footer>
  <p id="rodape"></p>
</footer>
</div>
<div class="tooltip" id="tip" role="status"></div>

<script>
const D = __DADOS__;
const tip = document.getElementById('tip');
const rs = v => v == null ? '—' : 'R$ ' + Math.round(v).toLocaleString('pt-BR');
const dbr = s => { const [a,m,d] = s.split('-'); return d+'/'+m; };
const dbrf = s => { const [a,m,d] = s.split('-'); return d+'/'+m+'/'+a; };
const dur = m => m ? Math.floor(m/60)+'h'+String(m%60).padStart(2,'0') : '—';
const nome = c => D.nomes[c] || c;

function mostraTip(ev, html){
  tip.innerHTML = html; tip.classList.add('on');
  const x = Math.min(ev.clientX + 14, window.innerWidth - 270);
  tip.style.left = x + 'px';
  tip.style.top = Math.max(8, ev.clientY - 12) + 'px';
}
function escondeTip(){ tip.classList.remove('on'); }
document.addEventListener('scroll', escondeTip, true);

/* ---------- cabecalho ---------- */
document.getElementById('subtitulo').textContent =
  (D.janela.somente_ida ? 'Somente ida' : 'Ida e volta') +
  ' · partidas de ' + dbrf(D.janela.de) + ' a ' + dbrf(D.janela.ate);
const cota = D.cota;
document.getElementById('meta').innerHTML = [
  '<span class="chip">atualizado ' + D.gerado_em + '</span>',
  '<span class="chip">' + D.totais.ofertas.toLocaleString('pt-BR') + ' ofertas no histórico</span>',
  '<span class="chip' + (cota.usado >= cota.parar_em ? '' : ' on') + '">cota ' +
    cota.usado + '/' + cota.limite + ' · ' + cota.mes + '</span>'
].join('');

/* ---------- tiles ---------- */
const H = D.destaques;
const tiles = [];
if (H.mais_barato) tiles.push({hl:true, k:'Trecho mais barato agora',
  v: rs(H.mais_barato.preco),
  n: H.mais_barato.origem+' → '+H.mais_barato.destino+' · '+dbrf(H.mais_barato.data)});
if (H.melhor_total) tiles.push({k:'Melhor com deslocamento',
  v: rs(H.melhor_total.total),
  n: H.melhor_total.origem+' → '+H.melhor_total.destino+' · passagem '+rs(H.melhor_total.preco)});
if (H.queda) tiles.push({k:'Maior queda em 48h', v: rs(H.queda.delta),
  n: H.queda.rota+' · de '+rs(H.queda.de)+' para '+rs(H.queda.para)+
     ' ('+Math.round(H.queda.pct*100)+'%)'});
if (H.melhor_origem) tiles.push({k:'Origem mais barata', v: H.melhor_origem[0],
  n: nome(H.melhor_origem[0])+' · a partir de '+rs(H.melhor_origem[1])});
if (H.melhor_destino) tiles.push({k:'Destino mais barato', v: H.melhor_destino[0],
  n: nome(H.melhor_destino[0])+' · a partir de '+rs(H.melhor_destino[1])});
if (H.melhor_data) tiles.push({k:'Melhor data de partida', v: dbr(H.melhor_data[0]),
  n: 'a partir de '+rs(H.melhor_data[1])});
document.getElementById('tiles').innerHTML = tiles.length ? tiles.map(t =>
  '<div class="tile'+(t.hl?' hl':'')+'"><span class="k">'+t.k+'</span>'+
  '<span class="v">'+t.v+'</span><span class="n">'+t.n+'</span></div>').join('')
  : '<div class="tile"><span class="k">Aguardando</span><span class="v">—</span>'+
    '<span class="n">Ainda não há preços coletados. Os números aparecem depois da primeira rodada.</span></div>';

/* ---------- mapa de calor ---------- */
function nivel(preco, base){
  if (!preco || !base) return null;
  const r = preco / base;
  if (r <= 0.75) return -3; if (r <= 0.86) return -2; if (r <= 0.93) return -1;
  if (r <= 1.07) return 0;  if (r <= 1.18) return 1;  if (r <= 1.32) return 2;
  return 3;
}
const sufixo = n => (n < 0 ? '-' + Math.abs(n) : String(n));
const corNivel = n => n === null ? 'var(--surface-2)' : 'var(--dv-' + sufixo(n) + ')';
const inkNivel = n => n === null ? 'var(--muted)'    : 'var(--dvi-' + sufixo(n) + ')';

let html = '<thead><tr><th class="rota">Rota</th>' +
  D.datas.map(d=>'<th>'+dbr(d)+'</th>').join('') + '</tr></thead><tbody>';
for (const linha of D.grade){
  html += '<tr><th class="rota">'+linha.rota+'</th>';
  for (const c of linha.celulas){
    const n = nivel(c.preco, linha.base);
    const rot = c.preco ? rs(c.preco).replace('R$ ','') : '·';
    html += '<td class="cell" data-rota="'+linha.rota+'" data-data="'+c.data+
      '" data-preco="'+(c.preco||'')+'" data-total="'+(c.total||'')+
      '" data-base="'+(linha.base||'')+'"><span style="background:'+corNivel(n)+
      ';color:'+inkNivel(n)+'">'+rot+'</span></td>';
  }
  html += '</tr>';
}
document.getElementById('heat').innerHTML = html + '</tbody>';

document.getElementById('heat').addEventListener('mousemove', ev => {
  const td = ev.target.closest('td.cell'); if (!td) return escondeTip();
  const p = Number(td.dataset.preco), b = Number(td.dataset.base);
  if (!p) return mostraTip(ev, td.dataset.rota+' · '+dbrf(td.dataset.data)+
    '<br>sem preço coletado ainda');
  const dif = b ? Math.round((1 - p/b)*100) : null;
  mostraTip(ev, '<b>'+td.dataset.rota+'</b> · '+dbrf(td.dataset.data)+
    '<br>passagem '+rs(p)+
    (td.dataset.total ? '<br>com deslocamento '+rs(Number(td.dataset.total)) : '')+
    (b ? '<br>normal da rota '+rs(b)+'<br>'+(dif>0? dif+'% abaixo' : Math.abs(dif)+'% acima') : ''));
});
document.getElementById('heat').addEventListener('mouseleave', escondeTip);

document.getElementById('legenda').innerHTML =
  '<span>mais barato que o normal</span>' +
  [-3,-2,-1,0,1,2,3].map(n=>'<i style="background:'+corNivel(n)+'"></i>').join('') +
  '<span>mais caro</span><span style="margin-left:10px">· <i style="background:var(--surface-2);border:1px solid var(--line)"></i> sem dado</span>';

/* ---------- grafico ---------- */
const selRota = document.getElementById('selRota');
const rotas = Object.keys(D.series);
selRota.innerHTML = rotas.length ? rotas.map(r=>'<option>'+r+'</option>').join('')
  : '<option>sem dados</option>';
selRota.addEventListener('change', ()=>desenhar(selRota.value));

function desenhar(rota){
  const svg = document.getElementById('grafico');
  const pts = D.series[rota] || [];
  const W = svg.clientWidth || 900, Hh = 260, m = {t:16,r:16,b:26,l:56};
  if (pts.length < 2){
    svg.innerHTML = '<text x="'+(W/2)+'" y="130" class="lbl" text-anchor="middle">'+
      'Ainda não há dias suficientes para desenhar a linha.</text>'; return;
  }
  const vals = pts.map(p=>p.preco);
  const min = Math.min(...vals)*0.96, max = Math.max(...vals)*1.04;
  const x = i => m.l + i*(W-m.l-m.r)/(pts.length-1);
  const y = v => m.t + (max-v)/(max-min)*(Hh-m.t-m.b);
  const linha = pts.map((p,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(p.preco).toFixed(1)).join(' ');
  const area = linha+' L '+x(pts.length-1).toFixed(1)+' '+(Hh-m.b)+' L '+m.l+' '+(Hh-m.b)+' Z';
  let s = '';
  for (let g=0; g<=3; g++){
    const v = min + (max-min)*g/3, yy = y(v);
    s += '<line class="grid" x1="'+m.l+'" x2="'+(W-m.r)+'" y1="'+yy+'" y2="'+yy+'"/>'+
         '<text class="lbl" x="'+(m.l-8)+'" y="'+(yy+3.5)+'" text-anchor="end">'+rs(v)+'</text>';
  }
  s += '<path class="area" d="'+area+'"/><path class="serie" d="'+linha+'"/>';
  const ult = pts.length-1;
  s += '<circle class="pt" cx="'+x(ult)+'" cy="'+y(pts[ult].preco)+'" r="4.5"/>';
  s += '<text class="lbl" x="'+m.l+'" y="'+(Hh-8)+'">'+dbr(pts[0].dia)+'</text>';
  s += '<text class="lbl" x="'+(W-m.r)+'" y="'+(Hh-8)+'" text-anchor="end">'+dbr(pts[ult].dia)+'</text>';
  svg.innerHTML = s;

  svg.onmousemove = ev => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(pts.length-1,
      Math.round((ev.clientX - r.left - m.l)/((W-m.l-m.r)/(pts.length-1)))));
    mostraTip(ev, '<b>'+rota+'</b><br>'+dbrf(pts[i].dia)+'<br>menor preço '+rs(pts[i].preco));
  };
  svg.onmouseleave = escondeTip;
}
if (rotas.length) desenhar(rotas[0]);
window.addEventListener('resize', ()=>{ if(rotas.length) desenhar(selRota.value); });

/* ---------- tabela de oportunidades ---------- */
const fO = document.getElementById('fOrigem'), fD = document.getElementById('fDestino'),
      fS = document.getElementById('fScore');
const uniq = k => [...new Set(D.ofertas.map(o=>o[k]))].sort();
fO.innerHTML = '<option value="">todas</option>' + uniq('origem').map(v=>'<option>'+v+'</option>').join('');
fD.innerHTML = '<option value="">todos</option>' + uniq('destino').map(v=>'<option>'+v+'</option>').join('');
[fO,fD,fS].forEach(el=>el.addEventListener('change', pintarTabela));

function pintarTabela(){
  const lista = D.ofertas.filter(o =>
    (!fO.value || o.origem===fO.value) &&
    (!fD.value || o.destino===fD.value) &&
    o.score >= Number(fS.value))
    .sort((a,b) => b.score - a.score);   // as melhores primeiro
  const t = document.getElementById('tabela');
  if (!lista.length){ t.innerHTML = '<tbody><tr><td class="vazio">Nada aqui ainda.</td></tr></tbody>'; return; }
  t.innerHTML = '<thead><tr><th>Score</th><th>Rota</th><th>Partida</th><th>Preço</th>'+
    '<th>Com deslocamento</th><th>Desconto</th><th>Cia</th><th>Escalas</th><th>Duração</th>'+
    '<th>Bagagem</th><th></th></tr></thead><tbody>' +
    lista.map(o => '<tr>'+
      '<td><span class="pill '+o.classe+'">'+Math.round(o.score)+' · '+o.classe+'</span></td>'+
      '<td class="mono">'+o.origem+'→'+o.destino+'</td>'+
      '<td class="mono">'+dbrf(o.data)+'</td>'+
      '<td class="n">'+rs(o.preco)+'</td>'+
      '<td class="n">'+rs(o.total)+'</td>'+
      '<td class="n">'+(o.desconto>0? Math.round(o.desconto*100)+'%' : '—')+'</td>'+
      '<td>'+(o.companhia||'—')+'</td>'+
      '<td class="n">'+o.escalas+'</td>'+
      '<td class="n">'+dur(o.duracao)+'</td>'+
      '<td>'+o.bagagem+'</td>'+
      '<td>'+(o.link? '<a href="'+o.link+'" target="_blank" rel="noopener">abrir</a>':'')+'</td>'+
    '</tr>').join('') + '</tbody>';
}
pintarTabela();

/* ---------- alertas ---------- */
const ta = document.getElementById('tabAlertas');
ta.innerHTML = D.alertas.length
  ? '<thead><tr><th>Enviado</th><th>Rota</th><th>Partida</th><th>Preço</th><th>Score</th><th>Motivo</th></tr></thead><tbody>'+
    D.alertas.map(a=>'<tr><td class="mono">'+a.enviado_em.replace('T',' ')+'</td>'+
      '<td class="mono">'+a.origem+'→'+a.destino+'</td>'+
      '<td class="mono">'+dbrf(a.data_partida)+'</td>'+
      '<td class="n">'+rs(a.preco)+'</td><td class="n">'+Math.round(a.score)+'</td>'+
      '<td>'+(a.motivo||'')+'</td></tr>').join('')+'</tbody>'
  : '<tbody><tr><td class="vazio">Nenhum alerta enviado ainda — é assim que deve ser até aparecer uma oportunidade de verdade.</td></tr></tbody>';

document.getElementById('rodape').textContent =
  'Orçamento de referência: ' + rs(D.totais.orcamento) + ' por trecho. ' +
  'Custo de deslocamento considerado: ' +
  Object.entries(D.deslocamento).map(([k,v])=>k+' '+rs(v)).join(' · ') + '.';
</script>
</body>
</html>
"""
