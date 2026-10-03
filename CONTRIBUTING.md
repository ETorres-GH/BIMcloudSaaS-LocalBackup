# Contributing

- Before sending code, open an [issue](../../issues) and describe what you plan to do. English or
  Portuguese are both fine.
- Never include credentials, internal addresses or project files (`.pln`, `.BIMProject`,
  `.BIMLibrary`) in issues, commits or pull requests.
- Code, names and comments are in English. Texts the user sees go in
  `src/bimcloud_backup/locales/en.json` and `pt-BR.json`, never straight in the code.
- Before the pull request, run `ruff check .`, `ruff format --check .` and `pytest`.
- Sign each commit with `git commit -s`
  ([Developer Certificate of Origin](https://developercertificate.org/)).

## Licensing of contributions

This project uses the [PolyForm Shield License 1.0.0](LICENSE). By sending a contribution, you
declare that:

1. the contribution is your own work, or you have the right to send it;
2. it is licensed under the same terms as the project; and
3. you grant the maintainer, **Ettore Torres**, a perpetual, worldwide, non-exclusive, free and
   irrevocable license to use, modify, sublicense and distribute the contribution, including
   under other license terms.
