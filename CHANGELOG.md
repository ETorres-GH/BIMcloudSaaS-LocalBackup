# Changelog

Mudanças do programa. As versões seguem o [Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [Unreleased]

### Adicionado

- Janela única: situação dos backups à esquerda, ajustes à direita.
- Login pela página do BIMcloud, com verificação em duas etapas.
- Backup dos arquivos do BIMcloud numa pasta com data e hora.
- Exportação de projetos (`.BIMProject` e/ou o último `.pln`) e bibliotecas (`.BIMLibrary`),
  com opção de incluir os snapshots do BIMcloud.
- Escolha das pastas do BIMcloud a copiar.
- Arquivos que não mudaram não são baixados de novo.
- Histórico por dias, sempre com um mínimo de backups, ou só o último backup.
- Limites de duração máxima e de espaço livre em disco.
- Botão **Cancelar backup**.
- Backup automático diário ou a cada N minutos.
- Log diário.
- Executável `BIMcloudBackup.exe` com ícone próprio e arquivo `.sha256` para conferir o download.
- Guia do usuário e resumo em inglês no README.

### Segurança

- Senhas e chaves de acesso nunca aparecem nas mensagens nem no log.
- O login só é enviado ao próprio BIMcloud, por https.
- Um download que falha não deixa arquivo pela metade.
- Nomes de arquivo que o Windows não aceita são renomeados, sem sobrescrever nada.

### Alterado

- A janela acompanha a escala do Windows e cabe em telas de 1366x768.
- O usuário do BIMcloud passou a ser opcional.
