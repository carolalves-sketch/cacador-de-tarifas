"""Armazenamento do historico e calculo das estatisticas de preco.

Por que CSV e nao um banco binario:
o historico e gravado em arquivos de texto (data/ofertas.csv). Assim o Git
guarda apenas as linhas novas de cada rodada em vez de um arquivo binario
inteiro, o repositorio nao incha, e voce consegue abrir o historico direto
no Excel. Para as consultas, o CSV e carregado num SQLite em memoria a cada
execucao - rapido ate centenas de milhares de linhas.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import statistics
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .config import PASTA_DADOS

ARQ_OFERTAS = PASTA_DADOS / "ofertas.csv"
ARQ_ALERTAS = PASTA_DADOS / "alertas.csv"
ARQ_ESTADO = PASTA_DADOS / "estado.json"

FUSO_BR = timezone(timedelta(hours=-3))


def agora_br() -> datetime:
    return datetime.now(FUSO_BR)


# ------------------------------------------------------------------
#  Modelo de uma oferta
# ------------------------------------------------------------------
@dataclass
class Oferta:
    coletado_em: str
    fonte: str                 # "google_flights" | "travelpayouts"
    origem: str
    destino: str
    data_partida: str          # YYYY-MM-DD
    preco: float
    moeda: str = "BRL"
    companhia: str = ""
    companhias: str = ""       # todas as cias do itinerario, separadas por |
    numero_voo: str = ""
    escalas: int = 0
    conexoes: str = ""         # ex.: "CDG 1h50"
    conexao_min_minutos: int = 0
    duracao_min: int = 0       # duracao total em minutos
    partida_iso: str = ""
    chegada_iso: str = ""
    bagagem: str = "desconhecida"   # despachada | mao | nenhuma | desconhecida
    link: str = ""
    id_oferta: str = ""
    data_volta: str = ""       # vazio quando somente ida

    def __post_init__(self) -> None:
        if not self.id_oferta:
            self.id_oferta = self.calcular_id()

    def calcular_id(self) -> str:
        crua = "|".join(
            [
                self.origem,
                self.destino,
                self.data_partida,
                self.companhia,
                self.numero_voo,
                str(self.escalas),
                str(int(self.duracao_min)),
            ]
        )
        return hashlib.sha1(crua.encode("utf-8")).hexdigest()[:12]

    @property
    def chave_alerta(self) -> str:
        """Identidade usada para nao repetir o mesmo alerta."""
        return f"{self.origem}|{self.destino}|{self.data_partida}|{self.companhia}"

    @property
    def rota(self) -> str:
        return f"{self.origem}-{self.destino}"

    def hora_partida(self) -> int | None:
        return _hora_de(self.partida_iso)

    def hora_chegada(self) -> int | None:
        return _hora_de(self.chegada_iso)


def _hora_de(iso: str) -> int | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "")).hour
    except ValueError:
        try:
            return int(iso.split(" ")[1].split(":")[0])
        except (IndexError, ValueError):
            return None


CAMPOS = [f.name for f in fields(Oferta)]


# ------------------------------------------------------------------
#  Repositorio
# ------------------------------------------------------------------
class Historico:
    def __init__(self) -> None:
        PASTA_DADOS.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self._criar_tabelas()
        self._carregar_csv()

    # ---------- estrutura ----------
    def _criar_tabelas(self) -> None:
        colunas = ", ".join(f"{c} TEXT" for c in CAMPOS if c not in ("preco", "escalas", "duracao_min", "conexao_min_minutos"))
        self.con.execute(
            f"""CREATE TABLE ofertas (
                    {colunas},
                    preco REAL,
                    escalas INTEGER,
                    duracao_min INTEGER,
                    conexao_min_minutos INTEGER
                )"""
        )
        self.con.execute(
            """CREATE TABLE alertas (
                    enviado_em TEXT, chave TEXT, id_oferta TEXT,
                    origem TEXT, destino TEXT, data_partida TEXT,
                    preco REAL, score REAL, motivo TEXT
               )"""
        )
        self.con.execute("CREATE INDEX ix_rota ON ofertas (origem, destino, data_partida)")
        self.con.commit()

    def _carregar_csv(self) -> None:
        if ARQ_OFERTAS.exists():
            with open(ARQ_OFERTAS, newline="", encoding="utf-8") as f:
                linhas = [self._normalizar(r) for r in csv.DictReader(f)]
            if linhas:
                marcadores = ", ".join(["?"] * len(CAMPOS))
                self.con.executemany(
                    f"INSERT INTO ofertas ({', '.join(CAMPOS)}) VALUES ({marcadores})",
                    [tuple(l.get(c) for c in CAMPOS) for l in linhas],
                )
        if ARQ_ALERTAS.exists():
            with open(ARQ_ALERTAS, newline="", encoding="utf-8") as f:
                registros = list(csv.DictReader(f))
            if registros:
                self.con.executemany(
                    "INSERT INTO alertas VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            r.get("enviado_em"), r.get("chave"), r.get("id_oferta"),
                            r.get("origem"), r.get("destino"), r.get("data_partida"),
                            _float(r.get("preco")), _float(r.get("score")), r.get("motivo"),
                        )
                        for r in registros
                    ],
                )
        self.con.commit()

    @staticmethod
    def _normalizar(linha: dict) -> dict:
        linha = dict(linha)
        linha["preco"] = _float(linha.get("preco"))
        for c in ("escalas", "duracao_min", "conexao_min_minutos"):
            linha[c] = int(_float(linha.get(c)))
        return linha

    # ---------- escrita ----------
    def gravar_ofertas(self, ofertas: Iterable[Oferta]) -> int:
        ofertas = list(ofertas)
        if not ofertas:
            return 0
        novo = not ARQ_OFERTAS.exists()
        with open(ARQ_OFERTAS, "a", newline="", encoding="utf-8") as f:
            escritor = csv.DictWriter(f, fieldnames=CAMPOS)
            if novo:
                escritor.writeheader()
            for o in ofertas:
                escritor.writerow(asdict(o))
        marcadores = ", ".join(["?"] * len(CAMPOS))
        self.con.executemany(
            f"INSERT INTO ofertas ({', '.join(CAMPOS)}) VALUES ({marcadores})",
            [tuple(getattr(o, c) for c in CAMPOS) for o in ofertas],
        )
        self.con.commit()
        return len(ofertas)

    def gravar_alerta(self, oferta: Oferta, score: float, motivo: str) -> None:
        registro = {
            "enviado_em": agora_br().isoformat(timespec="seconds"),
            "chave": oferta.chave_alerta,
            "id_oferta": oferta.id_oferta,
            "origem": oferta.origem,
            "destino": oferta.destino,
            "data_partida": oferta.data_partida,
            "preco": oferta.preco,
            "score": round(score, 1),
            "motivo": motivo,
        }
        novo = not ARQ_ALERTAS.exists()
        with open(ARQ_ALERTAS, "a", newline="", encoding="utf-8") as f:
            escritor = csv.DictWriter(f, fieldnames=list(registro))
            if novo:
                escritor.writeheader()
            escritor.writerow(registro)
        self.con.execute(
            "INSERT INTO alertas VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(registro.values()),
        )
        self.con.commit()

    # ---------- estatisticas ----------
    def precos(
        self,
        origem: str | None = None,
        destino: str | None = None,
        data_partida: str | None = None,
        janela_dias: int = 60,
    ) -> list[float]:
        corte = (agora_br() - timedelta(days=janela_dias)).isoformat()
        sql = "SELECT preco FROM ofertas WHERE coletado_em >= ? AND preco > 0"
        args: list = [corte]
        if origem:
            sql += " AND origem = ?"
            args.append(origem)
        if destino:
            sql += " AND destino = ?"
            args.append(destino)
        if data_partida:
            sql += " AND data_partida = ?"
            args.append(data_partida)
        return [r["preco"] for r in self.con.execute(sql, args)]

    def estatisticas(self, precos: list[float]) -> dict:
        if not precos:
            return {"n": 0}
        ordenado = sorted(precos)
        return {
            "n": len(ordenado),
            "minimo": ordenado[0],
            "maximo": ordenado[-1],
            "media": statistics.fmean(ordenado),
            "mediana": statistics.median(ordenado),
            "p10": percentil(ordenado, 0.10),
            "p25": percentil(ordenado, 0.25),
        }

    def resumo_rota(self, origem: str, destino: str, janela_dias: int = 60) -> dict:
        base = self.estatisticas(self.precos(origem, destino, janela_dias=janela_dias))
        base["ultimos_7"] = self.estatisticas(self.precos(origem, destino, janela_dias=7)).get("mediana")
        base["ultimos_30"] = self.estatisticas(self.precos(origem, destino, janela_dias=30)).get("mediana")
        if base.get("ultimos_7") and base.get("ultimos_30"):
            base["variacao_pct"] = (base["ultimos_7"] - base["ultimos_30"]) / base["ultimos_30"]
        else:
            base["variacao_pct"] = None
        return base

    def melhor_por_celula(self, janela_dias: int = 3) -> list[sqlite3.Row]:
        """Menor preco atual de cada combinacao origem/destino/data."""
        corte = (agora_br() - timedelta(days=janela_dias)).isoformat()
        return list(
            self.con.execute(
                """SELECT origem, destino, data_partida,
                          MIN(preco) AS preco, COUNT(*) AS n
                     FROM ofertas
                    WHERE coletado_em >= ? AND preco > 0
                 GROUP BY origem, destino, data_partida""",
                [corte],
            )
        )

    def alertas_de_hoje(self) -> int:
        hoje = agora_br().date().isoformat()
        cur = self.con.execute(
            "SELECT COUNT(*) c FROM alertas WHERE substr(enviado_em,1,10) = ?", [hoje]
        )
        return cur.fetchone()["c"]

    def ultimo_alerta(self, chave: str) -> sqlite3.Row | None:
        cur = self.con.execute(
            "SELECT * FROM alertas WHERE chave = ? ORDER BY enviado_em DESC LIMIT 1", [chave]
        )
        return cur.fetchone()

    def ultimo_alerta_da_rota(self, origem: str, destino: str) -> sqlite3.Row | None:
        cur = self.con.execute(
            """SELECT * FROM alertas WHERE origem = ? AND destino = ?
               ORDER BY enviado_em DESC LIMIT 1""",
            [origem, destino],
        )
        return cur.fetchone()

    def alertas_recentes(self, limite: int = 30) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM alertas ORDER BY enviado_em DESC LIMIT ?", [limite]
        )]

    def serie_historica(self, origem: str, destino: str) -> list[tuple[str, float]]:
        return [
            (r["dia"], r["preco"])
            for r in self.con.execute(
                """SELECT substr(coletado_em,1,10) AS dia, MIN(preco) AS preco
                     FROM ofertas WHERE origem = ? AND destino = ? AND preco > 0
                 GROUP BY dia ORDER BY dia""",
                [origem, destino],
            )
        ]

    def total_ofertas(self) -> int:
        return self.con.execute("SELECT COUNT(*) c FROM ofertas").fetchone()["c"]


def percentil(ordenado: list[float], p: float) -> float:
    if not ordenado:
        return 0.0
    if len(ordenado) == 1:
        return ordenado[0]
    pos = p * (len(ordenado) - 1)
    baixo = int(pos)
    alto = min(baixo + 1, len(ordenado) - 1)
    peso = pos - baixo
    return ordenado[baixo] * (1 - peso) + ordenado[alto] * peso


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------
#  Estado (cota, rodizio das datas, marcas de tempo)
# ------------------------------------------------------------------
class Estado:
    PADRAO = {
        "mes_cota": "",
        "consumo_cota": 0,
        "indice_grade": 0,
        "ultimo_comparativo": "",
        "ultimo_aviso_falha": "",
    }

    def __init__(self) -> None:
        PASTA_DADOS.mkdir(parents=True, exist_ok=True)
        if ARQ_ESTADO.exists():
            self.dados = {**self.PADRAO, **json.loads(ARQ_ESTADO.read_text(encoding="utf-8"))}
        else:
            self.dados = dict(self.PADRAO)
        mes = agora_br().strftime("%Y-%m")
        if self.dados.get("mes_cota") != mes:
            self.dados["mes_cota"] = mes
            self.dados["consumo_cota"] = 0
            self.salvar()

    def salvar(self) -> None:
        ARQ_ESTADO.write_text(
            json.dumps(self.dados, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def __getitem__(self, chave: str):
        return self.dados.get(chave)

    def __setitem__(self, chave: str, valor) -> None:
        self.dados[chave] = valor
        self.salvar()
