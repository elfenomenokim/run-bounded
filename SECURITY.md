# Security policy

## Supported versions

Security fixes go into the latest release. Upgrade before reporting, in case the problem is
already fixed.

| Version | Supported |
| --- | --- |
| 0.1.2 (unreleased fixes included) | Yes |
| Older | No |

## Reporting a vulnerability

Report it privately, not in a public issue: use the
[Report a vulnerability](https://github.com/elfenomenokim/run-bounded/security/advisories/new) form
on the repository's Security tab.

Include the command you ran, the version (`run-bounded --version`), your platform, and what an
attacker could do with the result. You get a reply within a few days, and you hear when a fix is
out. Say how you want to be credited if you want a name in the release note.

## In scope

- A child process that survives a timeout or idle limit, because the process group or the Windows
  Job Object was not terminated.
- A grandchild, a daemon or a background process that escapes the limit in any other way.
- An output cap that a child can exceed, or output that reaches the parent's disk or memory
  without the documented per-stream cap.
- A result line that reports `ok` for a child that timed out, failed or died from a signal.
- Environment variables or file descriptors that reach the child beyond the documented minimal set
  and the parent's own standard streams.

## Not a vulnerability

- Running an untrusted command in the bounded child. The tool runs the command you name; that
  command's behaviour is yours to judge, exactly as if you had called it directly.
- A child that reads files it can already read as your user.
- Refusing a relative executable path that contains `/` or `\`. That rejection is intended.

## Disclosure

Once a fix is released, the advisory is published with credit to the reporter unless they prefer
otherwise.
