"""The optional mail relay (core profile `smtp`), as the manager configures it.

`settings` reads what the core stored and holds the rules for changing it, `probe` talks
SMTP (to the mail server behind the relay, and to the relay), `status` asks the core's
`doctor` how the relay is doing, and `service` turns a validated request into the jobs
that apply it. The core's `setup` stores the values; nothing here writes the
configuration directory.
"""
