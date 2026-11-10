# Kwai Analyzer

Painel local para Windows que analisa perfis do **Kwai**, **TikTok**, **Instagram Reels** e **YouTube Shorts**: coleta views, curtidas, comentários, duração e descrição de cada vídeo, monta um ranking com filtros, exporta CSV/JSON e baixa os vídeos escolhidos na maior qualidade disponível, com áudio.

Tudo roda no seu computador. O app abre uma janela própria do Chrome ou do Edge para cada plataforma; se o site pedir login ou verificação, você resolve nessa janela e a coleta continua. Nenhum dado é enviado a terceiros.

## Requisitos

- Windows 10 ou 11
- Google Chrome ou Microsoft Edge instalado
- [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) no PATH (recomendado): junta áudio e vídeo nos downloads em alta qualidade do TikTok, Instagram e YouTube. Uma forma rápida de instalar: `winget install Gyan.FFmpeg`

Não é preciso instalar Python: na primeira execução o app baixa um Python portátil e as dependências para a pasta `runtime`.

## Instalação

1. Baixe o projeto: botão **Code → Download ZIP** no GitHub (e extraia), ou `git clone` deste repositório.
2. Dê dois cliques em `INICIAR-COM-DIAGNOSTICO.bat`. A primeira execução demora alguns minutos.
3. Deixe a janela preta aberta enquanto usa o programa. O painel abre em http://127.0.0.1:8000 (ou use `ABRIR-PAINEL.bat`).

Para atualizar o componente do YouTube, use `ATUALIZAR-YOUTUBE.bat`. Se o painel já estiver em execução durante uma atualização, feche a janela preta e abra novamente; depois atualize a página.

## Uso responsável

Este projeto é para análise e uso pessoal. Baixe apenas conteúdo que você tem direito de usar, respeite os direitos autorais dos criadores e os termos de uso de cada plataforma. Os perfis de navegador criados pelo app (pasta `data`) guardam o seu login: não compartilhe essa pasta.

## Licença

[GPL-3.0](LICENSE). Você pode usar, estudar, modificar e redistribuir; versões modificadas distribuídas precisam manter o código aberto sob a mesma licença.

## Coleta e Ranking

1. Informe o perfil e o máximo de vídeos aprovados no ranking. O padrão tenta chegar ao fim, com teto de 20.000 vídeos. Limites menores produzem um ranking dos vídeos coletados até aquele ponto, não necessariamente dos melhores vídeos de todo o perfil.
2. Escolha Views mínimas (padrão original: 100 mil), hashtags exatas opcionais e ordenação antes de iniciar. Separe as hashtags por espaços, vírgulas ou ponto e vírgula, com ou sem `#` (exemplo: `#novela, #humor`). Basta o vídeo conter uma delas; maiúsculas e minúsculas são equivalentes, mas `#novela` não corresponde a `#novelas`. Deixe o campo vazio para não filtrar por hashtag. Vídeos abaixo do mínimo ou sem nenhuma das hashtags são desconsiderados; o coletor continua procurando até atingir o máximo de aprovados ou encerrar. Para incluir todas as views, escolha Qualquer quantidade. Alterar esses critérios exige uma nova coleta.
3. No Ranking, combine busca, hashtags exatas, mínimos/máximos de visualizações, curtidas e comentários, disponibilidade de link/mídia e fonte. Altere a ordenação e o Top sem refazer a coleta.
4. CSV e JSON da tela exportam todos os resultados filtrados. A seleção também inclui todas as páginas filtradas; seleções fora dos filtros são informadas. Copiar links usa a seleção quando houver, ou todos os resultados filtrados quando não houver seleção.

No filtro da seção Ranking, as hashtags informadas são combinadas com E (todas obrigatórias). Na seção Analisar e criar ranking, basta uma das hashtags informadas. Métricas desconhecidas aparecem como `—`, ficam por último na ordenação e não atendem a filtros numéricos. O Ranking mostra até 100 linhas por página.

## Correções da coleta

- Vídeos com IDs distintos não são unidos por título, visualizações ou capa repetida.
- Índices de identidade aceleram a identificação em perfis grandes.
- A área rolável é reavaliada depois que os cards aparecem e durante tentativas sem progresso; a rolagem avança gradualmente para não saltar grades virtualizadas.
- O coletor espera mais por carregamentos lentos e tenta novamente antes de parar.
- Respostas sem cursor, JSON inválido, itens rejeitados e capturas iniciadas em páginas posteriores recorrem à coleta visual.
- Parada por limite, interrupção, estagnação e fim informado pela API têm mensagens distintas. Estagnação visual não comprova fim do perfil.
- Coleta e resolução de links não podem executar simultaneamente. O salvamento da última análise usa substituição atômica.

O diagnóstico anterior de `@realityshow816` registrava 10 vídeos vistos, zero no ranking e fim após três rodadas, com uma área de rolagem sem altura disponível. As correções cobrem essas condições, mas a quantidade total desse perfil precisa ser comparada em uma nova coleta real. Login, vídeos privados/indisponíveis e limitações da própria página podem afetar o que ela apresenta.

Os rankings antigos continuam legíveis, mas vídeos que foram descartados pelo filtro de coleta da versão anterior exigem nova coleta. Os endpoints de exportação direta preservam a exportação integral; os botões do painel exportam o recorte filtrado. Os parâmetros antigos de análise permanecem aceitos para compatibilidade.

## Filtro de duração

No Ranking, informe **Duração mínima (segundos)** e/ou **Duração máxima (segundos)**; por exemplo, 60 e 180 para vídeos de 1 a 3 minutos, incluindo os limites. Campos vazios mantêm todos os vídeos. O filtro combina com os anteriores e vale para seleção e exportação. A coluna Duração mostra minutos e segundos (ou horas nos vídeos longos).

A duração é registrada a partir do manifesto de vídeo do Kwai (`transcode_manifest_info.adaptationSet[].duration`, em milissegundos), de `ext_params.video`, de outros campos com unidade reconhecida ou do indicador da coleta visual. A duração da música não é usada. Valores ausentes aparecem como `—` e não atendem ao filtro de duração.

Para rankings já coletados, clique em **Atualizar durações**. O app consulta pequenos trechos dos metadados dos MP4 com mídia direta disponível, sem baixar os vídeos completos. O ranking, os links e as seleções são preservados; durações já conhecidas não são substituídas. O botão de parar permite interromper e salvar o progresso. Mídias indisponíveis continuam com `—`. Reinicie o servidor do aplicativo e atualize a página para carregar esta correção.

## Verificação dos testes

Na pasta do projeto:

```powershell
.\runtime\python\python.exe -m unittest discover -s tests -v
node --test tests/ranking.test.js
.\runtime\python\python.exe tests/check_ui.py
```

O teste de interface exige Chrome instalado e usa dados isolados, sem alterar o ranking salvo. Verifica filtros, paginação, seleção, exportação, apresentação móvel e descoberta de uma área rolável que aparece após o carregamento inicial. Esses testes não comprovam a completude de perfis reais.

## Filtros de coleta restaurados (2.2.2)

Views mínimas, hashtag e ordenação voltaram à grade original de quatro campos. Os filtros do Ranking são independentes e só refinam o que a coleta aprovou. Os arquivos visuais e scripts usam URLs versionadas para evitar misturar HTML atualizado com arquivos antigos do cache.

## Confiabilidade e desempenho (2.3)

- Resolução em lote: a grade é lida uma vez por rodada e indexada por identidade. Links presentes no card e IDs conhecidos dispensam cliques. Apenas cards encontrados recebem tentativas de clique (até duas por execução).
- Captura de âncora, window.open, History API, navegação imediata e popup; abas auxiliares e observadores são liberados ao terminar ou interromper.
- Título e views iguais não são usados para atribuir links. Correspondências ambíguas ficam pendentes; o motivo aparece ao passar o mouse sobre "pendente" e no JSON/diagnóstico.
- O progresso da busca de links é salvo também em falhas. Falhas de links automáticos não descartam a coleta. O navegador pode ser reaberto após ser fechado.
- Paginação direta tem timeout de 15 segundos; erros continuam pelo fallback visual. A fila de respostas tem tamanho limitado.
- A interface evita redesenhar resultados iguais e atualiza a lista no máximo a cada dois segundos durante coleta. Status continua atualizado separadamente.
- Arquivos visuais e scripts recebem uma versão derivada de seu conteúdo, evitando cache antigo após novas alterações.
- Downloads são gravados em blocos em arquivo temporário e só substituem o MP4 final após a conclusão. Páginas de erro e respostas vazias são rejeitadas.

Teste adicional dos cinco modos de abertura de links, usando páginas locais isoladas:

```powershell
.\runtime\python\python.exe tests/check_links_browser.py
```

Os testes de links e desempenho usam cenários controlados. Um link resolvido é um endereço identificado, não uma garantia de reprodução ou disponibilidade no Kwai. Não há promessa de coletar vídeos que o perfil não disponibiliza na sessão atual.

## Fim da grade e links pendentes (2.3.1)

O diagnóstico de @jackass.brasil registrou 85 vídeos e cursor `no_more`, mas o início da coleta por API ocorreu após a primeira página. Agora esse sinal é preservado e combinado com a chegada ao rodapé após a recuperação visual das páginas anteriores. Avisos explícitos de fim na página também são reconhecidos. Na ausência deles, quatro rodadas estáveis no rodapé de uma grade rolável, sem indicador de carregamento, encerram como fim dos vídeos disponíveis na grade. Essa última evidência é uma inferência sobre a grade atual, não comprovação de todos os vídeos da conta. O diagnóstico registra `end_evidence`.

Respostas reais de feed recebidas durante a rolagem são aproveitadas para enriquecer os vídeos com IDs e links. Na resolução de pendentes, a segunda tentativa usa o mouse real do navegador, atendendo cards que exigem eventos de usuário. Dados antigos são preservados: use Coletar / repetir Top para tentar novamente os links; faça nova coleta para validar o término.

## Correção operacional 2.3.2

Corrigida a leitura de links que subia até a linha da grade e podia associar a âncora de um vídeo vizinho. URLs e IDs agora são extraídos apenas de um escopo que contém uma única mídia. Thumbnail divergente invalida a correspondência, inclusive quando a chave é igual. Após rolar até um card, sua identidade é conferida novamente antes do clique real. Um card DOM não pode sobrescrever a capa de um item da API com uma thumbnail diferente.

A busca de links passa a encerrar por falta de cards novos, mesmo se a rolagem continuar se movimentando: até oito rodadas sem novidade, 12 segundos sem progresso entre rodadas e orçamento total de 23 a 90 segundos por execução (dependendo da quantidade de pendentes). Uma chamada de navegador já em curso ainda pode consumir seu timeout. Links não resolvidos permanecem pendentes. No coletor visual, mudanças de altura sozinhas não reiniciam a contagem de progresso. Capturas iniciadas no meio do perfil tentam primeiro reiniciar a paginação da API, evitando repetir toda a coleta por rolagem quando isso funciona.

Rankings anteriores sem versão de associação têm links de itens com thumbnail retirados da memória ao restaurar, porque associações antigas podem estar incorretas; o arquivo original não é modificado na leitura. Refaça a análise para reconstruir o ranking, pois vídeos unidos incorretamente em versões anteriores não podem ser recuperados só repetindo a busca de links. Os filtros e o padrão visual foram preservados.


## TikTok — análise e download em massa

O TikTok funciona como o Kwai: o próprio app abre uma janela de navegador (Chrome ou Edge), com perfil guardado em `data/tiktok/browser-profile`. Não há extensão para instalar.

**Primeira vez:** em **Plataforma**, escolha **TikTok — download em massa**, clique em **Abrir navegador do TikTok** e faça login na janela. O login fica salvo para as próximas vezes.

**Usar:** cole o perfil (@usuario ou URL), ajuste os filtros e clique em **Iniciar coleta**. O app abre o perfil, rola a página e lê o feed que ela recebe, com descrição, views, curtidas, comentários, duração e link de cada vídeo. A coleta percorre o perfil inteiro antes de montar o ranking; **Máximo no ranking** só limita quantos vídeos aparecem depois de ordenar. Deixe a janela aberta. Se aparecer uma verificação, resolva nela, e a coleta continua. Ela para se nenhum vídeo novo chegar por 5 minutos.

Os arquivos ficam em `downloads/tiktok`, no melhor stream que o feed oferece (até 1080p, às vezes em H.265). Quando esse stream vem sem som, o app junta o áudio de outra fonte do mesmo vídeo com o FFmpeg, sem perder qualidade, preferindo o som original do autor. Até 5 vídeos baixam ao mesmo tempo, cada um em partes paralelas, e o painel mostra o tempo total. Publicações de fotos são ignoradas, e vídeos já baixados não são baixados de novo. Os links de vídeo vencem: se o download falhar, refaça a coleta. O ranking fica em `data/tiktok/latest.json`.

## Instagram Reels — análise e download em massa

Funciona como o TikTok: o app abre uma janela própria de navegador, com perfil guardado em `data/instagram/browser-profile`.

**Primeira vez:** em **Plataforma**, escolha **Instagram Reels — download em massa**, clique em **Abrir navegador do Instagram** e faça login na janela. O login fica salvo.

**Usar:** cole o perfil (@usuario ou https://www.instagram.com/usuario), ajuste os filtros e clique em **Iniciar coleta**. O app abre a aba **Reels** do perfil, rola até o fim e lê descrição, reproduções, curtidas, comentários e duração de cada reel. Fotos e carrosséis ficam de fora. A coleta termina quando o Instagram informa que não há mais reels, ou depois de 45 segundos rolando sem nenhum reel novo.

Os arquivos ficam em `downloads/instagram`, na maior resolução oferecida. Quando a melhor versão vem sem som, o app junta o melhor áudio disponível com o FFmpeg, sem recomprimir. Os downloads usam as mesmas partes paralelas do TikTok. Se o ranking foi carregado de uma sessão anterior, o app abre cada reel em segundo plano para buscar um link novo. O ranking fica em `data/instagram/latest.json`.

## YouTube Shorts — análise e download em massa

Reinicie o servidor e atualize a página. Em **Plataforma**, escolha **YouTube Shorts — download em massa**. Cole um canal (@nome ou URL), ajuste os filtros e clique em **Iniciar coleta**. O canal sempre será consultado pela aba `/shorts`, inclusive quando o endereço informado termina em `/videos` ou `/streams`. Playlists e vídeos avulsos são recusados.

Apenas vídeos provenientes da aba Shorts entram no ranking. Duração curta, formato vertical e hashtag #shorts não bastam para aprovar um vídeo. Rankings antigos sem essa confirmação são ocultados da tela; refaça a coleta. O arquivo antigo não é apagado durante a leitura. A fila também recusa itens sem confirmação de Shorts.

Os filtros, ranking, Top, exportações e seleção em massa continuam disponíveis. O mínimo inicial é 100 mil views; escolha **Qualquer quantidade** para incluir Shorts menores. Hashtags vêm do título e da descrição. Métricas ausentes permanecem desconhecidas. O limite considera os Shorts aprovados pelos filtros, e a ordenação considera apenas os já coletados.

Use **Selecionar filtrados** e **Baixar selecionados**. Os arquivos ficam em `downloads/youtube`. A fila continua após falhas individuais e permite **Parar downloads**. Cada Short é baixado na maior resolução disponível (normalmente 1080×1920), preferindo H.264 + AAC em MP4 para abrir em qualquer editor; o nome do arquivo inclui a resolução (ex.: `youtube-ID-1080x1920.mp4`). Isso exige o FFmpeg — no PATH, em `runtime\ffmpeg.exe` ou em `runtime\ffmpeg\bin\ffmpeg.exe`. Sem FFmpeg o YouTube só oferece 360p, e o painel avisa.

A coleta lista a aba Shorts de forma rápida e consulta curtidas, comentários e duração de vários vídeos em paralelo. O `cookies.txt` é usado apenas quando o YouTube pede a verificação "não sou um robô": cookies vencidos fazem o YouTube recusar o vídeo ("The page needs to be reloaded") e esconder os formatos HD. Se precisar dele, exporte um arquivo novo.

Os rankings permanecem separados: `data/latest.json` para Kwai e `data/youtube/latest.json` para Shorts. Trocar de plataforma não interrompe operações. Se a extração deixar de funcionar após mudanças do YouTube, execute `ATUALIZAR-YOUTUBE.bat` e reinicie o app.

Testes cobrem filtros, limite de aprovados, persistência, cancelamento, falhas individuais, bloqueio de vídeos comuns e rankings antigos. O teste optativo `runtime\python\python.exe tests/check_youtube_live.py` coleta e baixa um Short público em pasta temporária, sem alterar seus rankings.
