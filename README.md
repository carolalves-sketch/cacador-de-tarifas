# Caçador de Tarifas — Brasil → Europa

Monitor automático de passagens **somente ida**, saindo de Brasília, São Paulo ou
Rio de Janeiro para Amsterdã, Copenhague, Frankfurt, Munique, Berlim ou Viena,
com partida entre **13 e 23 de abril de 2027**.

Ele roda sozinho cinco vezes por dia na nuvem do GitHub, guarda o histórico de
preços, calcula se cada oferta é realmente uma promoção e te avisa no Telegram
apenas quando vale a pena. Também publica um painel na web que se atualiza a
cada rodada.

**Custo: R$ 0 por mês.** Tudo funciona dentro das camadas gratuitas.

---

## Índice

1. [O que você precisa fazer (30 minutos, uma vez só)](#1-o-que-você-precisa-fazer)
2. [Como o sistema funciona](#2-como-o-sistema-funciona)
3. [Mudando as configurações](#3-mudando-as-configurações)
4. [Rodando manualmente](#4-rodando-manualmente)
5. [Onde ficam os dados](#5-onde-ficam-os-dados)
6. [Quando algo dá errado](#6-quando-algo-dá-errado)
7. [Perguntas frequentes](#7-perguntas-frequentes)

---

## 1. O que você precisa fazer

São quatro contas gratuitas e quatro senhas para colar. Nenhuma pede cartão de
crédito. Faça na ordem.

### Passo 1 — Conta no SerpApi (preços reais do Google Voos)

1. Entre em **https://serpapi.com/users/sign_up** e crie a conta gratuita.
2. Depois de confirmar o e-mail, vá em **https://serpapi.com/manage-api-key**.
3. Copie o valor do campo **Your Private API Key**. É um texto longo de letras
   e números. Guarde num bloco de notas — vamos usar daqui a pouco.

O plano gratuito dá **250 buscas por mês**. O sistema tem uma trava que para em
245, então nunca haverá cobrança.

### Passo 2 — Bot do Telegram (por onde chegam os alertas)

1. No Telegram, procure por **@BotFather** (é o robô oficial, tem selo azul).
2. Mande a mensagem `/newbot`.
3. Ele pede um nome (pode ser `Caçador de Tarifas`) e depois um nome de usuário
   que precisa terminar em `bot` (por exemplo `carol_tarifas_bot`).
4. Ele responde com uma mensagem contendo **o token**, parecido com
   `8123456789:AAF7x...`. Copie e guarde.
5. Agora procure por **@userinfobot** e mande qualquer mensagem. Ele responde
   com o seu **Id**, um número. Copie e guarde — esse é o `chat id`.
6. **Importante:** procure o bot que você acabou de criar e mande `/start` para
   ele. Sem isso o Telegram não deixa o bot te mandar mensagem.

### Passo 3 — Conta no Travelpayouts (varredura gratuita)

1. Entre em **https://www.travelpayouts.com/** e crie a conta gratuita
   (é um programa de afiliados; você não precisa divulgar nada).
2. Vá em **https://app.travelpayouts.com/programs** e ative o programa
   **Aviasales**.
3. O token fica no seu perfil, em **Profile → API token** — ou direto em
   **https://app.travelpayouts.com/profile**. Copie e guarde.

### Passo 4 — Repositório no GitHub

1. Se ainda não tiver conta, crie em **https://github.com/signup**.
2. Clique em **New repository**, dê o nome `cacador-de-tarifas`, marque
   **Private** e crie.
3. Na página do repositório vazio, clique em **uploading an existing file**.
4. Descompacte o arquivo `cacador-de-tarifas.zip` no seu computador e **arraste
   todas as pastas e arquivos de dentro dele** para a área de upload.
   Confira que subiram: `src/`, `.github/`, `config.yaml`, `requirements.txt`.
5. Clique em **Commit changes**.

> Se a pasta `.github` não aparecer ao arrastar, é porque o Windows esconde
> pastas que começam com ponto. Ative **Exibir → Itens ocultos** no Explorador
> de Arquivos e arraste de novo.

### Passo 5 — Colar as quatro chaves

No repositório, vá em **Settings → Secrets and variables → Actions** e clique em
**New repository secret**. Crie um por vez, com estes nomes exatos:

| Nome do secret        | O que colar                                  |
|-----------------------|----------------------------------------------|
| `SERPAPI_KEY`         | a chave do passo 1                           |
| `TELEGRAM_TOKEN`      | o token do BotFather (passo 2)               |
| `TELEGRAM_CHAT_ID`    | o número do @userinfobot (passo 2)           |
| `TRAVELPAYOUTS_TOKEN` | o token do Travelpayouts (passo 3)           |

Depois de salvos, nem você nem eu conseguimos ver esses valores de novo — só
substituir. É assim que deve ser.

### Passo 6 — Ligar

1. Aba **Actions** → se aparecer um aviso pedindo confirmação, clique em
   **I understand my workflows, go ahead and enable them**.
2. Aba **Settings → Pages** → em **Source** escolha **Deploy from a branch**,
   selecione a branch `main` e a pasta **`/docs`**. Salve.
   Em poucos minutos seu painel estará no ar em
   `https://SEU-USUARIO.github.io/cacador-de-tarifas/`.

### Passo 7 — Primeiro teste

1. Aba **Actions** → clique em **Caçador de Tarifas** na lista da esquerda.
2. Clique em **Run workflow**, escolha o modo `varredura` e confirme.
3. Em um ou dois minutos a execução termina com um ✓ verde. Clique nela para
   ver o log — deve aparecer algo como
   `Varredura BSB-AMS: 47 ofertas na janela.`
4. Rode de novo, agora no modo `grade`. Esse gasta 4 das suas 250 buscas
   mensais e é o que traz preço real.

Pronto. A partir daí ele roda sozinho nos horários programados.

---

## 2. Como o sistema funciona

### As cinco rodadas do dia (horário de Brasília)

| Horário | Modo         | O que faz                                              | Custo |
|---------|--------------|--------------------------------------------------------|-------|
| 06:00   | `varredura`  | Travelpayouts varre as 18 rotas × todos os dias        | grátis |
| 08:00   | `grade`      | Google Voos confirma 4 datas com preço real            | 4 buscas |
| 13:00   | `varredura`  | segunda varredura do dia                                | grátis |
| 14:00   | `zoom`       | confirma o candidato mais fora do padrão                | 0 ou 1 busca |
| 20:00   | `varredura`  | terceira varredura                                      | grátis |
| 21:00   | `grade`      | confirma mais 3 datas                                   | 3 buscas |

Total: cerca de **241 buscas por mês** contra a cota de 250.

As 11 datas de partida entram em rodízio: cada rodada paga pega as próximas da
fila, e a grade inteira é revisada com preço real a cada **36 horas**.

### Como ele decide o que é promoção

Cada oferta recebe uma nota de 0 a 100:

| Fator                   | Pontos |
|-------------------------|--------|
| Preço contra o histórico| 60     |
| Escalas                 | 12     |
| Duração do voo          | 10     |
| Horários                | 8      |
| Bagagem                 | 5      |
| Qualidade do itinerário | 5      |

Um voo direto, com bagagem e horário ótimo, mas com preço normal, chega a no
máximo 40 pontos. Para passar de 75 é preciso estar **realmente barato**.

Um alerta só sai quando:

- a nota é **75 ou mais** *e* o preço está pelo menos **12% abaixo** do normal
  daquela rota; **ou**
- o preço está abaixo de **R$ 1.500** (e a nota é pelo menos 60); **ou**
- a nota é **90 ou mais**; **ou**
- o preço é tão baixo que parece **erro de tarifa** — nesse caso o alerta avisa
  do risco de cancelamento.

Acima de **R$ 2.800** ele não alerta, a não ser que o desconto seja de 35% ou mais.

Enquanto o histórico é curto (primeiras duas semanas), o sistema fica mais
exigente: pede nota 85 e desconto de 20%. Isso evita alertas errados no começo,
quando ele ainda não sabe o que é caro.

### Como ele evita repetir alertas

Cada alerta fica gravado. O mesmo voo só é avisado de novo se o preço cair pelo
menos 8% (ou R$ 150), se a nota subir 8 pontos, se mudar de faixa, ou depois de
7 dias como lembrete. Há ainda um limite de **4 alertas por dia** e um intervalo
mínimo de **6 horas** por rota.

---

## 3. Mudando as configurações

Tudo o que você pode querer mudar está no arquivo **`config.yaml`**, na raiz do
repositório. Para editar pelo navegador: abra o arquivo no GitHub, clique no
lápis ✏️, altere e clique em **Commit changes**. A próxima rodada já usa.

Os ajustes mais prováveis:

```yaml
viagem:
  partida_de:  "2027-04-13"     # primeira data de partida
  partida_ate: "2027-04-23"     # última data de partida
  somente_ida: true             # false volta a buscar ida e volta

orcamento:
  referencia: 1800              # aparece na mensagem como marcador
  teto_absoluto: 2800           # acima disso quase nunca alerta
  chao_alerta: 1500             # abaixo disso alerta sempre

alertas:
  score_minimo: 75              # aumente se estiver recebendo demais
  maximo_por_dia: 4

origens:
  - { codigo: GRU, ..., custo_deslocamento: 450 }   # quanto custa chegar lá
```

**Voltar a buscar ida e volta:** troque `somente_ida: true` por `false`. As
durações de 7 a 20 dias já estão no arquivo, logo abaixo. Só lembre que o
consumo de cota sobe bastante — talvez valha reduzir
`datas_por_rodada_grade` de 4 para 2.

**Acrescentar um destino:** adicione uma linha na lista `destinos` com o código
do aeroporto e o nome. Ex.: `- { codigo: LIS, nome: "Lisboa" }`.

---

## 4. Rodando manualmente

**Pelo GitHub (recomendado):** aba **Actions → Caçador de Tarifas → Run
workflow**, escolha o modo e confirme.

**No seu computador**, se algum dia quiser:

```bash
pip install -r requirements.txt

# no Windows (PowerShell):
$env:SERPAPI_KEY="..."; $env:TRAVELPAYOUTS_TOKEN="..."
$env:TELEGRAM_TOKEN="..."; $env:TELEGRAM_CHAT_ID="..."

python -m src.run --modo varredura     # camada gratuita
python -m src.run --modo grade         # gasta 4 buscas
python -m src.run --modo painel        # só regenera o painel
python -m src.run --modo simular       # dados de teste, não gasta nada
python -m src.run --modo grade --sem-alerta   # busca mas não manda nada
```

Para conferir se a lógica de pontuação está sã:

```bash
python tests/test_scoring.py
```

---

## 5. Onde ficam os dados

| Arquivo               | O que é                                                        |
|-----------------------|----------------------------------------------------------------|
| `data/ofertas.csv`    | **o histórico**. Toda oferta já vista. Abre no Excel.          |
| `data/alertas.csv`    | todos os alertas enviados, com preço, nota e motivo            |
| `data/estado.json`    | contador de cota e posição do rodízio de datas                 |
| `docs/index.html`     | o painel publicado                                             |
| `logs/AAAA-MM.log`    | registro de cada rodada                                        |

O histórico é guardado em CSV de propósito: assim o Git grava só as linhas novas
de cada rodada (o repositório não incha), e você consegue abrir o arquivo direto
no Excel para fazer suas próprias contas.

---

## 6. Quando algo dá errado

O sistema é feito para falhar sem quebrar: se uma fonte cai, ele registra e
continua com a outra; se as duas caem, ele te manda **um** aviso no Telegram
(no máximo um por dia) e o histórico continua intacto.

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Execução vermelha com `Faltam segredos` | uma das quatro chaves não foi salva ou o nome está diferente | confira em Settings → Secrets; os nomes precisam ser exatamente os da tabela do passo 5 |
| `SerpApi recusou a chave (401)` | chave copiada com espaço ou incompleta | copie de novo em serpapi.com/manage-api-key |
| `Cota do SerpApi esgotada` | passou de 250 buscas no mês | é normal perto do fim do mês; volta sozinho na virada. A varredura gratuita continua rodando |
| Nenhum alerta há semanas | pode estar certo | confira o painel: se as células estão todas em tom neutro, não houve promoção. Se quiser mais sensibilidade, baixe `score_minimo` para 70 |
| Alertas demais | limite baixo demais para a rota | suba `score_minimo` para 80 ou `desconto_minimo` para 0.18 |
| Bot não manda mensagem | você não mandou `/start` para ele | abra o bot no Telegram e mande `/start` |
| Painel não abre | Pages ainda não configurado | Settings → Pages → branch `main`, pasta `/docs` |

Para ver o que aconteceu numa rodada: **Actions** → clique na execução →
**rodada** → abra o passo *Executar o monitor*. O log fica lá, em português.

---

## 7. Perguntas frequentes

**Ele compra a passagem sozinha?**
Não, e é de propósito. O alerta traz o link; a compra é sua. Automatizar compra
exigiria deixar dados de pagamento num robô, e não vale o risco.

**O preço do alerta é garantido?**
Não. Passagem aérea é inventário vivo — o valor pode mudar entre o alerta e a
compra. Em promoção forte, minutos importam.

**Por que às vezes ele não alerta um preço baixo?**
Porque baixo em reais não é a mesma coisa que baixo para aquela rota. Frankfurt
a R$ 1.750 pode ser o preço de sempre; Amsterdã a R$ 1.880 pode ser promoção.
Ele compara cada rota com o histórico dela mesma.

**Só de ida compensa?**
Nem sempre. Em rotas Brasil–Europa o trecho sozinho costuma custar entre 60% e
90% do valor da ida e volta. Por isso o sistema faz, uma vez por semana, uma
busca de ida e volta só para você comparar — o resultado aparece no log e no
histórico.

**Passagem só de ida dá problema na imigração?**
Pode. Países do Espaço Schengen às vezes pedem comprovação de saída do bloco
dentro do prazo permitido. Vale ter uma resposta pronta no embarque.

**Quanto isso vai custar?**
Nada, na configuração atual. Se um dia quiser mais cobertura, o plano de
**US$ 25/mês** do SerpApi dá 1.000 buscas — o suficiente para revisar a grade
inteira três vezes por dia. Nesse caso, mude no `config.yaml`:
`limite_mensal: 1000`, `parar_em: 980` e `datas_por_rodada_grade: 11`.

---

## Fontes usadas

- **Google Flights via [SerpApi](https://serpapi.com/google-flights-api)** —
  preço real, companhia, escalas, bagagem e link. Uma busca cobre as três
  origens e os seis destinos ao mesmo tempo.
- **[Aviasales Data API (Travelpayouts)](https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API)** —
  varredura gratuita e ilimitada, com dados de cache de até 7 dias. Nunca gera
  alerta sozinha.

A Amadeus, que seria a escolha natural, **encerrou o portal Self-Service em 17
de julho de 2026**. Skyscanner e Kiwi só liberam acesso por parceria comercial.
Kayak e Momondo não têm API pública de preços. Nenhuma fonte aqui envolve
raspagem de sites que proíbem esse tipo de acesso.
