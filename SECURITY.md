# Security Policy

## Scope

Carrac is published as a reference release (v0.226.22).

**In scope:** the code in this repository.

**Not in scope:** any hosted instance of Carrac run by someone else; do not
test against systems you do not own. Third-party services the code
integrates with (exchanges, data providers, payment or model vendors)
belong to their vendors — report issues in those to the vendor.

## Reporting a vulnerability

Report privately through GitHub: the repository's **Security** tab →
**Report a vulnerability**. Please do not open a public issue for a
security bug.

Include what the issue is, how to reproduce it, what an attacker gets
out of it, and the commit you looked at.

## What to expect

Reports are reviewed on a best-effort basis; there is no bug bounty. If
you would like credit, say so.

## If you run your own instance

You are the operator, and the security of your deployment is yours.

- **Set your own secrets.** `.env.example` ships placeholders and names
  the command that generates each real value. Never deploy the
  placeholders.
- **The account registered with `ADMIN_EMAIL` becomes an admin** and can
  sign in without verifying its email, so a fresh install works without
  outbound mail. Register that account before the instance is reachable
  by anyone else.
