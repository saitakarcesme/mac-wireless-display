# Security

The expected deployment is a single user's Mac and trusted Linux receiver connected by authenticated SSH. Keep listeners on loopback. Do not expose the raw video or refinement ports to a public or untrusted network.

Strict host-key checking is intentional. Establish and verify the first SSH connection through your normal administrative workflow. The project does not request private keys, disable certificate checks, edit the macOS TCC database or weaken login authentication.

Report security issues privately through the repository's security advisory workflow if it is enabled. If no private channel is available, open an issue requesting a contact without including exploit details or private data. Never attach credentials, captures or unredacted local logs.
