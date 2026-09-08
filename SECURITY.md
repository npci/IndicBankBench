# Security Policy

## Reporting a vulnerability

If you find a security vulnerability, please report it responsibly — do not open a public
issue.

Report it either:

- via the **Security advisories** page of this repository (private disclosure), or
- by email to **[npciai@npci.org.in](mailto:npciai@npci.org.in)**.

Please include a description of the issue, its impact, and steps to reproduce.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | yes       |

## Scope

- The evaluation harness (`indicbankbench/harness/`, `indicbankbench/scripts/`)
- Prompt-injection / jailbreak handling as it affects benchmark integrity

## Note on case data

All case data is synthetic. It is distributed as a dataset rather than in this repository; see
the README for how to fetch it. If you find that any fixture contains a real credential,
personal identifier, or other sensitive real-world data, report it privately as a vulnerability.
