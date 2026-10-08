# FiguresRadar

FiguresRadar prepara posts sobre promoções de anime figures para a conta X `@FiguresRadar`. O Buffer é o único responsável por escolher horários e publicar. **Nesta fase, a CLI funciona apenas em `DRY_RUN=true`: não cria, edita nem apaga posts.**

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
