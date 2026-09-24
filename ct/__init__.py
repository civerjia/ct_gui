"""CT filament/HV controller host software.

    ct.client    CTClient -- the Python API scripts use (ct_simple_control)
    ct.server    backend.py -- the HTTP server that owns the controller links
    ct.protocol  the framed UART/TCP protocol to the RP2350 controllers
    ct.update    self-update from GitHub at start-up
    ct.paths     where logs, state and the web UI live
    ct.analysis  offline tools (emission_plot)

Entry points kept at the top level so nothing that already uses them changes:
`python backend.py` and `from ct_simple_control import CTClient`.
"""
