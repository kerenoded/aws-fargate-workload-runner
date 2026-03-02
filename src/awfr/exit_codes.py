"""Typed exit codes for the AWFR runner.

0  SUCCESS        — run completed successfully
2  CONFIG_ERROR   — invalid configuration / validation error
3  AUTH_ERROR     — AWS authentication/authorization error
4  RUNTIME_ERROR  — runtime error during scenario execution
5  UNEXPECTED     — unexpected/unhandled error

Artifact upload failures are best-effort and MUST NOT change the exit code.
"""

SUCCESS = 0
CONFIG_ERROR = 2
AUTH_ERROR = 3
RUNTIME_ERROR = 4
UNEXPECTED = 5
