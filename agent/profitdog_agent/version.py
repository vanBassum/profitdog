"""What this build of the agent calls itself.

The agent is an EXE somebody downloaded weeks ago and forgot about. When it
misbehaves, the first question is always "which one is that?", and until now
nothing could answer it: the file is called `profitdog.exe` whatever tag built
it, and the process said nothing about itself on the way up.

So the version is stamped in at release time, reported in the first line of the
log, and sent to the server with every batch — three places to find it, for the
three ways the question gets asked (looking at the console, looking at the file,
looking at the account from another machine).

A checkout says `0.0.0+dev` and means it. That is not a placeholder to be
tidied up: a build that was never released should not claim a version it does
not have, because the whole point of the number is to identify the bytes.
"""

from __future__ import annotations

import os

#: Rewritten by the release workflow from the pushed tag. Left alone here.
__version__ = "0.0.0+dev"

#: The commit the release was built from, stamped in beside the version. A
#: version names a release; the SHA names the exact source, which is what you
#: want when two builds of "the same" tag ever disagree.
__commit__ = "unknown"


def agent_version() -> str:
    """This build's version, or an override for testing a release path."""
    return os.environ.get("PROFITDOG_VERSION") or __version__


def agent_commit() -> str:
    """The commit this build was made from, short form."""
    return (os.environ.get("PROFITDOG_COMMIT") or __commit__)[:12]


def agent_build() -> str:
    """Version and commit together, the way the agent introduces itself."""
    return f"{agent_version()} ({agent_commit()})"


__all__ = ["__commit__", "__version__", "agent_build", "agent_commit", "agent_version"]
