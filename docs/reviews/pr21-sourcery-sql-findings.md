# PR #21 Psycopg SQL finding review

Reviewed against `983f2dfc288412a83a8683eca3a8ee089d6faa54`, before the
server HTTPS policy follow-up. All 18 comments use the Opengrep
`python.sqlalchemy.security.sqlalchemy-execute-raw-query` rule, although
`scripts/server_database.py` uses Psycopg 3, not SQLAlchemy. Its `sql.SQL`
templates are constant. Variable object names are `sql.Identifier`, the
administrative `CREATE ROLE ... PASSWORD` value is `sql.Literal`, and data
queries use `%s` bindings. PostgreSQL cannot use a value placeholder for a
DDL identifier. The pinned disposable PostgreSQL regression
`test_adopted_database_overgrants_are_removed` uses a role name containing
SQL metacharacters and a quoted password, then verifies the application
table remains present and the role policy holds.

Each row is classified **false positive: safe Psycopg composition**. The
comment ID links to the original finding; line numbers refer to the reviewed
commit and may shift after this follow-up.

| Comment | Reviewed line | Variable input and protection |
| --- | ---: | --- |
| [4353649809](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649809) | 129–134 | Role name uses `Identifier`; password uses `Literal` in constant `CREATE ROLE`. |
| [4353649814](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649814) | 136–140 | Database and owner names use `Identifier`. |
| [4353649820](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649820) | 141–143 | Database name is an `Identifier`. |
| [4353649826](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649826) | 145–149 | Database and role names are `Identifier` values. |
| [4353649837](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649837) | 210–214 | Yoyo's fixed migration table name is an `Identifier`. |
| [4353649843](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649843) | 269–271 | Runtime role is a prebuilt `Identifier`. |
| [4353649848](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649848) | 272–276 | Yoyo table and runtime role are `Identifier` values. |
| [4353649851](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649851) | 277–279 | API role is a prebuilt `Identifier`. |
| [4353649857](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649857) | 281–285 | Table comes from a fixed tuple and is quoted with `Identifier`; worker role is an `Identifier`. |
| [4353649863](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649863) | 286–288 | Worker role is a prebuilt `Identifier`. |
| [4353649876](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649876) | 290–294 | Table comes from a fixed tuple and is quoted with `Identifier`; worker role is an `Identifier`. |
| [4353649884](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649884) | 296–298 | Table comes from a fixed tuple and is quoted with `Identifier`; dispatcher role is an `Identifier`. |
| [4353649888](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649888) | 299–303 | Dispatcher role is a prebuilt `Identifier`; column names are constant. |
| [4353649895](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649895) | 329–333 | Yoyo's fixed migration table name is an `Identifier`. |
| [4353649905](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353649905) | 389–391 | Enumerated public relation name is quoted with `Identifier`; unknown relation names are rejected. |
| [4353849255](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353849255) | 256–258 | Runtime role is a prebuilt `Identifier` in schema `REVOKE`. |
| [4353849265](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353849265) | 259–263 | Runtime role is a prebuilt `Identifier` in table `REVOKE`. |
| [4353849268](https://github.com/Amr-Mo-ali/Super-7/pull/21#discussion_r4353849268) | 264–268 | Runtime role is a prebuilt `Identifier` in sequence `REVOKE`. |

[Sourcery's triage documentation](https://docs.sourcery.ai/security/triage/)
identifies the supported narrowest false-positive action as **Ignore just the
selected findings**, with a reason, in its Issues dashboard. Each of these
18 findings needs that individual triage. Do not ignore the path, rule, or
repository; do not use `@sourcery-ai dismiss` or `resolve` to conceal a
blocking finding. The dashboard action is pending because this environment
has no connected Sourcery session or API credential. GitHub thread resolution
alone is not evidence that Sourcery's finding status changed.
