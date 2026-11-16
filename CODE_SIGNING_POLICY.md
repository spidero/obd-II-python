# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by
[SignPath Foundation](https://signpath.org/).

## What is signed

The Windows programs of the ready-to-run build – `OBDruid.exe` and `obdruid-cli.exe` in
`OBDruid-<version>-windows-x64.zip` attached to the [GitHub releases](https://github.com/spidero/obdruid/releases).
They are built by GitHub Actions (workflow [`build.yml`](.github/workflows/build.yml)) from the source code of the
tagged release in this repository; nothing is built or signed on a personal computer. Signing starts with the first
release after the SignPath Foundation has approved the project; earlier releases are not signed.

Only the project's own programs are signed. Third-party components bundled in the build (Python and its libraries)
keep the signatures of their authors, if they have any.

## Team roles

| Role | Members |
|---|---|
| Committers and reviewers | [Piotr Skwarna (@spidero)](https://github.com/spidero) |
| Approvers (approve each signing request) | [Piotr Skwarna (@spidero)](https://github.com/spidero) |

Every member uses multi-factor authentication for GitHub and SignPath.

## Privacy policy

This program will not transfer any information to other networked systems unless specifically requested by the
user or the person installing or operating it.

In detail: OBDruid talks to the OBD-II adapter it is connected to. Its web interface is served only to the local
computer (`127.0.0.1`) unless the user starts it with `--host` to reach it from other devices. Vehicle profiles,
history, settings and usage counters are stored only on the user's computer. There is no telemetry, no account and
no update check; links (GitHub, PayPal, e-mail) are opened only when the user clicks them.
