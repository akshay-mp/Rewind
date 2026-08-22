# Security Policy

## Supported Versions

Rewind is pre-1.0 software. Security fixes are targeted at the latest
published `0.x` release and the current `main` branch. Older releases and
development snapshots are not routinely supported; upgrade to the latest
release before requesting a fix where possible.

## Reporting a Vulnerability

Please use GitHub's private vulnerability reporting feature through the
repository's **Security** tab or [New private vulnerability
report](https://github.com/akshay-mp/agent-timetravel/security/advisories/new). Do not
report suspected vulnerabilities in public GitHub issues, pull requests, or
discussion posts.

Please include as much of the following as is safe to share:

- affected version, commit, package, and deployment context;
- a concise description of the security impact and attack prerequisites;
- reproducible steps or a minimal proof of concept;
- expected and observed behavior, including relevant logs or tracebacks;
- dependency, operating-system, and Python/Node version details; and
- any proposed mitigation or disclosure deadline.

Do not include credentials, API keys, personal data, production traces, or
other secrets. Redact sensitive material from examples and attachments.

## Response and Disclosure

The maintainers aim to acknowledge a report within 5 business days and provide
an initial triage update within 10 business days. These are response targets,
not guarantees; timing may vary with severity, reproducibility, and maintainer
availability.

Please allow time for investigation and remediation before public disclosure.
The maintainers will coordinate a disclosure timeline with the reporter,
request a CVE or GitHub Security Advisory where appropriate, and credit the
reporter only with their consent. If a report does not qualify as a security
vulnerability, the maintainers will explain the disposition when practical.
