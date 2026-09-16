"""Cookie-session auth: argon2 hashing, a session dependency, and three JSON routes.

No registration, no password reset, no OAuth — users exist only because `app/cli.py` put
them there. See §8 of the architecture doc for what was rejected and why.
"""
