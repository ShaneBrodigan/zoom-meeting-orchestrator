"""VM4 orchestrator: schedules a session, writes the spec, drives capture, builds
the manifest. Does NOT spawn bots — that is the clients' job (see client/).

See REFACTOR_DESIGN.md section 5 for the module map.
"""