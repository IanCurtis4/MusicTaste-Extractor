# Garantia de qualidade e critérios de aceite

Esta versão foi desenhada para que a suíte automatizada rode sem acesso à rede. As
integrações HTTP são exercitadas apenas com transportes ou resolvedores falsos e
fixtures sintéticas; nenhum teste envia histórico, título, consulta ou credencial a
um serviço externo.

## Matriz de aceite

| Área | Critério verificável | Cobertura automatizada |
|---|---|---|
| Banco | Schema v1 completo, inicialização idempotente, enums válidos e chaves estrangeiras ativas | `tests/test_db.py` |
| Takeout | Pesquisa, visualização, Unicode, canal ausente, HTML incompleto e timezone desconhecido | `tests/test_ingest.py` |
| Duplicação | Reimportar o mesmo arquivo não cria eventos; exportações sobrepostas mantêm ocorrências genuínas | `tests/test_ingest.py` e `tests/test_integration.py` |
| YouTube | Lotes de até 50, classificação auditável, item ausente, retomada, timeout, 429 e 5xx | `tests/test_youtube.py` |
| MusicBrainz | User-Agent, cache, rate limit, scoring, margem, empate e ausência de match | `tests/test_musicbrainz.py` |
| Revisão | Exportação/importação CSV, aceite, substituição e atomicidade de entrada inválida | `tests/test_review.py` e `tests/test_integration.py` |
| AllMusic | Bloqueio sem aceite explícito e testes sem acesso ao site real | `tests/test_cli.py` e testes do provedor |
| Relatório | Reconciliação de consumo versus intenção, taxonomias ponderadas e artefatos analíticos | `tests/test_integration.py` e testes de reporting |
| Privacidade | Saída padrão não contém consultas, títulos, URLs de atividade, caminhos de entrada ou chaves | `tests/test_cli.py` e `tests/test_integration.py` |
| CLI | Ajuda raiz e subcomandos públicos acessíveis | `tests/test_cli.py` |

Os dois arquivos locais indicados no projeto foram validados somente por contagens
agregadas: 8.596 pesquisas e 36.600 visualizações, ambas sem erro estrutural de
parsing. Esse ensaio não faz parte da suíte reproduzível porque os arquivos pessoais
não pertencem ao repositório.

## Execução offline e integrações opt-in

Execute a suíte completa com:

```powershell
uv run pytest
```

Os testes não requerem `YOUTUBE_API_KEY` nem `MUSIC_TASTE_CONTACT`; valores presentes
no ambiente devem ser removidos ou ignorados pelos testes. Uma falha que tente abrir
rede real deve ser tratada como defeito da suíte.

Em uso normal, YouTube e MusicBrainz são etapas explicitamente iniciadas pelo usuário
e enviam somente os identificadores ou campos necessários para resolução. O adaptador
AllMusic é experimental, nunca integra o pipeline padrão e exige
`--acknowledge-terms-risk`. O aceite apenas confirma ciência do risco: não autoriza
contornar `robots.txt`, CAPTCHA, autenticação, bloqueios ou termos do serviço.

## Auditoria manual futura

A precisão de resolução ainda não foi medida e não deve ser inferida do resultado dos
testes sintéticos. Antes de usar os matches em análises conclusivas:

1. Exporte uma amostra estratificada de 100 matches aceitos automaticamente, cobrindo
   faixas de score, anos, fontes (`watch`/`search`) e tipos de artista.
2. Faça revisão cega de artista, gravação e lançamento contra fontes confiáveis e
   registre correto/incorreto e motivo do erro.
3. Calcule precisão com intervalo de confiança e preserve a planilha de decisões como
   evidência da versão avaliada.
4. Exija ao menos 95% de precisão observada; se ficar abaixo, eleve os limiares de
   score/margem ou encaminhe a faixa problemática para revisão manual e repita a
   auditoria com uma nova amostra.

Essa auditoria é um gate futuro. A documentação não declara que a meta de 95% já foi
atingida.
