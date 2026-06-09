"""The subnet-client side of the harness (VM1/2/3/5).

A single long-running container runs the :mod:`client.agent` poller, which watches
S3 for a new session spec, matches its own private IP against the roster, and forks
a :mod:`client.bot` child to join the meeting. The bot reports its join/leave times
through :mod:`client.heartbeat`. See REFACTOR_DESIGN.md sections 5 and 9.
"""