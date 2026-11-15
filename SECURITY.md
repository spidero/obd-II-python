# Security policy

## Supported versions

Only the latest release on the `main` branch receives fixes.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through GitHub:
[Report a vulnerability](https://github.com/spidero/obdruid/security/advisories/new)
(the "Security" tab of the repository). You will get an answer as soon as possible.

## Things to know

- The web UI (`obdruid web`) has **no login**. With `--host 0.0.0.0` anyone who can reach the port
  can read live data and clear trouble codes (after a confirmation). Run it only on a trusted network
  (home LAN, VPN) and never expose it directly to the internet.
- OBDruid only uses the standard OBD-II diagnostic services. The only command that changes anything
  in the car is clearing trouble codes (mode 04), which also resets the readiness monitors.
