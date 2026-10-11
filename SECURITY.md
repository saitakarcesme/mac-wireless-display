# Security

The expected deployment is a single user's Mac and trusted Linux receiver connected by authenticated SSH. Keep listeners on loopback. Do not expose the raw video or refinement ports to a public or untrusted network.

Strict host-key checking is intentional. Establish and verify the first SSH connection through your normal administrative workflow. The project does not request private keys, disable certificate checks, edit the macOS TCC database or weaken login authentication.

Report security issues privately through the repository's security advisory workflow if it is enabled. If no private channel is available, open an issue requesting a contact without including exploit details or private data. Never attach credentials, captures or unredacted local logs.

## Temporary screen pixels

Settled-image refinement writes temporary raw BGRA pixel files on the Linux receiver for mpv to load. Files use mode `0600` inside a mode `0700` temporary directory. Normal processing unlinks each file after the renderer acknowledges it or loading fails; abrupt termination can leave files behind. These files contain screen content and must not be shared with logs or included in repository uploads. This cleanup is not a secure-erasure guarantee. No recording archive or project-hosted frame storage is created.
