# Descoberta de promoções

Estado da pesquisa em 2026-10-08. Esta fase consulta apenas HTML público e não envia dados ao Buffer ou ao X. Nenhuma API oficial de catálogo ou feed de promoções foi identificada nas seis lojas; isto não prova que não exista acesso mediante acordo comercial.

| Fonte | API/feed/JSON/sitemap | Página pública, preço, stock, IDs e imagem | Decisão |
| --- | --- | --- | --- |
| [AmiAmi](https://www.amiami.com/eng/search/list/?s_cate2=459&s_cate_tag=1&s_st_saleitem=1) | Nenhum endpoint oficial documentado identificado; robots.txt vazio | A página de saldos indexada mostra preço e nome, mas o acesso HTTP normal devolveu 403 | Excluída; não contornar a proteção |
| [Solaris Japan](https://solarisjapan.com/collections/special-offers) | Nenhum identificado | Há coleção pública de ofertas, mas os [termos](https://solarisjapan.com/pages/terms-of-service) proíbem spider/crawl/scrape | Excluída |
| [HobbyLink Japan](https://www.hlj.com/search/?GenreCode2=Action+Figures&MacroType2=Action+Figures&Page=1&Sort=std+desc&itemGroup=AUTUMNSALE2026) | Nenhum endpoint oficial identificado; robots.txt permite o catálogo | Listagem pública de sale e detalhe com preço atual, stock, código, JAN, fabricante e imagem. Preço anterior não está fiável no HTML | Implementada, limitada a 8 detalhes por execução |
| [Hobby Search](https://www.1999.co.jp/eng/campaign/sale/) | Nenhum identificado; robots.txt permite o catálogo | Páginas indexadas com preço, desconto, stock e imagens, mas acesso HTTP normal devolveu 403 | Excluída; não contornar a proteção |
| [Good Smile](https://www.goodsmile.com/en/anniversary_sale) | Nenhum identificado; robots.txt proíbe `/search` | Campanhas verificadas eram sobretudo cupões, sem preço final diretamente comparável por produto; disponibilidade pode depender de login | Excluída até haver fonte adequada |
| [Nin-Nin-Game](https://www.nin-nin-game.com/en/sales) | Nenhum endpoint oficial identificado; [sitemap](https://www.nin-nin-game.com/en/sitemap) público; robots.txt proíbe vários parâmetros mas permite categorias canónicas | [Saldos de scale figures](https://www.nin-nin-game.com/en/scale-figure-sales), [Nendoroids](https://www.nin-nin-game.com/en/nendoroid-sales) e [figmas](https://www.nin-nin-game.com/en/figma-sales) mostram preço anterior/atual, stock, imagem e ID; detalhe pode revelar mais dados | Implementada; uma página por categoria |

Os [termos da HLJ](https://support.hlj.com/hc/en-us/articles/115001722094-Terms-Conditions-of-Use) e os [termos da Nin-Nin](https://www.nin-nin-game.com/en/content/3-terms-and-conditions-of-use) foram consultados. A recolha mantém um User-Agent identificável, usa robots.txt, espera pelo menos 1,5 segundos entre pedidos por cliente, faz no máximo um retry em timeout ou 5xx e pára perante 403/429. A permissão de acesso público não equivale a autorização de reutilização das imagens; guardamos apenas URLs e verificamos `Content-Type` nos dez primeiros resultados.

## Pipeline

`python -m figuresradar discover` usa collectors isolados, normaliza produtos, remove stock esgotado e merchandising, elimina duplicados exatos por produto/loja/moeda e grava observações em `data/runtime/price-history.sqlite3`. O JSON completo é `data/runtime/latest-deals.json`. Ambos são locais e ignorados pelo Git. A CLI anterior de prévia do Buffer continua separada.

A identidade usa JAN, depois SKU do fabricante, depois ID do retailer. Sem IDs, preserva fabricante, personagem, série, escala, tipo, versão e título completo; variantes distintas não são fundidas por semelhança textual. Campos que a fonte não fornece ficam `null`.

O score com histórico suficiente usa até 45 pontos pela descida face à mediana de 30 dias, 20 pelo desconto anunciado, 15 por novo mínimo observado, 10 por frescura e 10 por qualidade dos dados. Exigimos cinco observações anteriores, um período observado de pelo menos sete dias até à observação atual e observações anteriores em pelo menos quatro dias distintos para confiança histórica HIGH. Antes disso, o modo cold start usa até 60 pontos pelo desconto anunciado, 10 por frescura e 10 por dados, limitado a 79; a razão explicita a falta de histórico. Uma promoção baseada apenas num MSRP elevado perde força quando a mediana observada confirma que o preço atual é habitual.

Os preços de moedas diferentes nunca são comparados. Algumas páginas Nin-Nin devolvem USD ou EUR conforme a sessão/localização; uma mudança de moeda inicia uma série histórica separada. Preços e stock podem mudar entre a listagem e a compra. A verificação de imagem comprova HTTPS e resposta com tipo `image/*`; a associação ao produto vem do cartão original e requer confirmação humana antes de qualquer publicação futura.

Qualidade: `90–100 EXCEPTIONAL`, `80–89 HOT`, `70–79 GOOD`, `60–69 FAIR`, `<60 IGNORE`. O limiar de futura elegibilidade é 70. Nada neste comando transforma resultados em posts.
