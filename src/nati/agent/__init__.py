"""The conversational agent: the tool loop, the tracer, and the system prompt.

Everything below the provider and above the web server lives here. The loop is a
generator of typed events, so the same code drives the web stream, a terminal
REPL, and the tests, with no branching on caller.
"""
