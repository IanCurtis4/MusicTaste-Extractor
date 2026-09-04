# Music Taste Extractor

Pipeline local para extrair eventos dos históricos do YouTube/YouTube Music
exportados pelo Google Takeout, reconhecer conteúdo musical, resolver faixas e
artistas e produzir uma análise reproduzível.

## Privacidade e fontes externas

- Os HTMLs, o banco, caches, CSVs de revisão e relatórios ficam fora do Git.
- Títulos, consultas e URLs não são impressos nos logs normais.
- A chave da YouTube Data API deve ser fornecida por `YOUTUBE_API_KEY` e nunca é
  persistida.
- O MusicBrainz exige um User-Agent identificável; defina
  `MUSIC_TASTE_CONTACT` com um e-mail ou URL de contato.
- O adaptador AllMusic é experimental, fica desativado por padrão e exige um
  aceite explícito dos riscos dos termos de uso. Ele não faz evasão de bloqueios.

## Instalação

Requer Python 3.12 a 3.14 e [`uv`](https://docs.astral.sh/uv/).

```powershell
uv sync --all-groups
uv run music-taste --help
```

## Fluxo recomendado

```powershell
uv run music-taste ingest `
  --search-history "C:\caminho\histórico de pesquisa.html" `
  --watch-history "C:\caminho\histórico-de-visualização.html"

$env:YOUTUBE_API_KEY = "sua-chave"
uv run music-taste enrich youtube

$env:MUSIC_TASTE_CONTACT = "seu-email-ou-url"
uv run music-taste resolve musicbrainz

uv run music-taste review export output\review.csv
# edite apenas as colunas de decisão documentadas no CSV
uv run music-taste review import output\review.csv

uv run music-taste report --output-dir output
```

O comando `run` reúne o fluxo padrão, mas nunca ativa o AllMusic:

```powershell
uv run music-taste run `
  --search-history "C:\caminho\histórico de pesquisa.html" `
  --watch-history "C:\caminho\histórico-de-visualização.html"
```

Use `uv run music-taste <comando> --help` para ver todas as opções.

## Metodologia

- Visualizações representam consumo provável, não uma garantia de reprodução
  completa.
- Pesquisas representam interesse/intencionalidade e não contam como plays.
- Apenas matches aceitos entram nos rankings canônicos.
- Distribuições taxonômicas dividem o peso de cada evento entre as tags da
  mesma taxonomia, evitando favorecer entidades com mais tags.

## Desenvolvimento

```powershell
uv run pytest
```
