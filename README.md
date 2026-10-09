# FiguresRadar

## Descoberta real (fase de leitura)

```bash
PYTHONPATH=src python3 -m figuresradar discover
```

Consulta páginas públicas de saldos da Nin-Nin-Game e da HobbyLink Japan, guarda histórico de preços em `data/runtime/price-history.sqlite3` e gera `data/runtime/latest-deals.json`. O diretório runtime está ignorado pelo Git. A saída inclui score, qualidade, stock, URL e motivo do score. Este comando não consulta nem escreve no Buffer; `DRY_RUN=true` e `BUFFER_WRITE_ENABLED=false` continuam os valores por defeito. As [fontes, limites e fórmula](docs/discovery.md) estão documentados em separado.

### Observação periódica local

O timer de utilizador em `ops/figuresradar-discover.timer` agenda a descoberta às 00:00, 06:00, 12:00 e 18:00 (hora local), com atraso aleatório até 20 minutos. O serviço usa `/usr/bin/python3`, `PYTHONPATH=src` absoluto e força as flags de publicação desativadas. A instalação na máquina persistente é:

```bash
mkdir -p ~/.config/systemd/user
ln -s /home/diogo/Downloads/dark-style-lab/figuresradar-repo/ops/figuresradar-discover.service ~/.config/systemd/user/figuresradar-discover.service
ln -s /home/diogo/Downloads/dark-style-lab/figuresradar-repo/ops/figuresradar-discover.timer ~/.config/systemd/user/figuresradar-discover.timer
systemctl --user daemon-reload
systemctl --user enable --now figuresradar-discover.timer
```

`PYTHONPATH=src python3 -m figuresradar history-status` mostra o estado da DB. `history-status --changes` mostra as mudanças da última recolha; `discover --changes` também as mostra. O lock `data/runtime/discover.lock` salta uma segunda execução sem interromper a primeira. `discovery_runs` e `discovery_run_sources` guardam o resultado e erro resumido por fonte. Uma migração do esquema atual cria antes `price-history.sqlite3.backup-<timestamp>` por meio da API de backup SQLite. A DB, backups, lock e JSON continuam fora do Git. O histórico HIGH requer cinco observações anteriores, pelo menos sete dias até à observação atual e observações anteriores em quatro dias distintos.

### Preparação de conteúdo sem publicação

`DRY_RUN=true BUFFER_WRITE_ENABLED=false PYTHONPATH=src python3 -m figuresradar prepare-posts` lê a fila real do canal FiguresRadar no Buffer e prepara apenas os deals do snapshot `data/runtime/latest-deals.json` com score ≥70, stock disponível e observação recente. Não chama a mutação de escrita. Para a consulta real da fila, fornecer `BUFFER_API_KEY` no ambiente; os IDs não secretos ficam em `config/buffer-target.json` e o canal é validado pela API.

Os resultados ficam em `data/runtime/prepared-posts/run-*/post-*/`, com `metadata.json`, `copy.txt` e `card.jpg` 1200×675. `PYTHONPATH=src python3 -m figuresradar review-posts` apresenta copy, URL, score e caminho do card; cada execução gera também `review.html`. As imagens são descarregadas dos hosts autorizados das lojas, validadas e guardadas em `data/runtime/assets/`, fora do Git. O card usa a imagem real do produto, sem geração AI.

As reservas dry run ficam em `data/runtime/preparation.sqlite3`, separadas do ledger de publicações. Impedem que uma nova preparação local repita a mesma oferta, mas aceitam uma descida material de pelo menos 5%. `prepare-posts --clear-test-reservations` remove apenas reservas locais `RESERVED`/`FAILED` para testes; não altera o ledger real nem apaga cards anteriores. Publicação continua desativada.

`NEKOPRICE_BASE_URL` está reservado para o futuro funil. Só `NEKOPRICE_CTA_URL`, quando explicitamente configurada com uma URL HTTPS existente, altera o destino da copy; por defeito, a copy liga à página da loja. Não se inventa `/deal/<id>`. Como os cards são locais e o Buffer exige uma imagem HTTPS pública, `buffer_payload` fica `null` e `buffer_payload_draft` regista `mode=addToQueue`, texto, canal e caminho local do card com estado `AWAITING_PUBLIC_CARD_URL`. Um URL público real e revalidação de preço/stock serão necessários antes de qualquer futura escrita. As descrições de personagem só são usadas se tiverem fonte HTTPS e confiança `HIGH`; sem dados fiáveis, são omitidas.

FiguresRadar prepara posts sobre promoções de anime figures para a conta X `@FiguresRadar`. O Buffer é o único responsável por escolher horários e publicar. **A CLI de prévia funciona apenas em `DRY_RUN=true`; a CLI de descoberta só lê lojas e grava dados locais. Nenhuma delas cria, edita ou apaga posts.**

## Configuração do Buffer

`config/buffer-target.json` fixa os IDs não secretos da organização e do canal `FiguresRadar` descobertos pela API do Buffer. Antes de ler a fila, a CLI confirma pela API que o ID corresponde ao canal X `FiguresRadar`. O token é recebido apenas por `BUFFER_API_KEY` no ambiente; não pertence ao repositório. Pode ser carregado de um ficheiro privado do utilizador, com permissão `0600`.

Na máquina de desenvolvimento, a credencial existente está em `~/.config/x-anime-publisher/buffer.env`. A CLI ignora os IDs desse ficheiro, que pertencem à ZahZah_cun, e usa os IDs confirmados em `config/buffer-target.json`.

```bash
set -a
. ~/.config/x-anime-publisher/buffer.env
set +a
DRY_RUN=true BUFFER_WRITE_ENABLED=false PYTHONPATH=src python3 -m figuresradar
```

Sem `BUFFER_API_KEY`, pode testar com `DRY_RUN=true PYTHONPATH=src python3 -m figuresradar --pending 8`. `--pending` simula apenas a contagem da fila. Os exemplos em `data/sample-deals.json` e `data/test-image.svg` são fictícios: `example.invalid` não aloja imagens.

## Fila e deduplicação

`needed_posts = max(0, 10 - pending_buffer_posts)`. `POSTS_PER_DAY=5` é apenas contexto; os horários são configurados no Buffer. O pipeline ordena ofertas, filtra stock/verificação, consulta o ledger SQLite e prepara no máximo `needed_posts`. A identidade de uma oferta inclui produto, loja, moeda e preço. Uma redução de pelo menos 5% face ao melhor preço já enviado pode originar uma oferta nova. Em dry run, o ledger é aberto só para leitura.

## Estrutura

- `src/figuresradar/config.py`: alvo da fila e proteções de escrita.
- `buffer_client.py`: seleção do canal, leitura da fila e mutação `addToQueue` preparada.
- `models.py`, `deduplication.py`, `persistence.py`: modelo, fingerprint de preparação e ledger SQLite.
- `deal_pipeline.py`, `content_generator.py`, `image_generator.py`: seleção e prévia determinísticas, com ponto substituível de revalidação.
- `live_publisher.py`: infraestrutura de escrita dormente, com uma única tentativa e reconciliação após resposta ambígua.
- `tests/`: testes locais; `.github/workflows/ci.yml`: CI de testes sem agendamento de posts.

Os ficheiros legados `products.json`, `sent_products.json` e `.github/workflows/ofertas.yml` foram preservados. `main.py` agora chama a CLI de dry run. `beautifulsoup4` permanece em `requirements.txt` por já existir no remoto; não é usado nesta fase.

## Proteções e ativação futura

A CLI aborta se `DRY_RUN=false`. O módulo de escrita exige **ambas** as condições `DRY_RUN=false` e `BUFFER_WRITE_ENABLED=true`, um cliente com escrita habilitada e um ledger gravável; também rejeita fixtures e URLs de teste. A mutação usa `mode: addToQueue`, sem `dueAt`. Depois de um timeout, consulta a fila e procura texto e imagem correspondentes. Se não conseguir confirmar, marca a tentativa como ambígua e bloqueia qualquer retry automático.

Para uma fase posterior: ligar fontes e preços reais, verificar stock/preço imediatamente antes de cada envio, alojar imagens autorizadas num URL HTTPS público, validar a mutação numa conta de teste e só depois considerar a ativação explícita do runner. Nenhum workflow de publicação foi ativado.

## Testes

```bash
python3 -m pip install -r requirements.txt
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
