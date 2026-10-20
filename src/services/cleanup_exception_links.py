"""Remove cleanup backlinks before joining an active failure to shutdown errors."""

from __future__ import annotations

from typing import NoReturn


def remove_primary_backlinks(cleanup: BaseException, primary: BaseException) -> BaseException:
    """Keep independent cleanup links while preventing a path back to primary."""
    if cleanup is primary:
        return primary
    if isinstance(cleanup, BaseExceptionGroup):
        removed, independent = cleanup.split(lambda member: member is primary)
        if removed is not None:
            if independent is None:
                return primary
            cleanup = independent

    pending = [cleanup]
    seen: set[int] = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        for attribute in ("__cause__", "__context__"):
            linked: BaseException | None = getattr(error, attribute)
            if linked is primary:
                setattr(error, attribute, None)
                continue
            if isinstance(linked, BaseExceptionGroup):
                removed, independent = linked.split(lambda member: member is primary)
                if removed is not None:
                    setattr(error, attribute, independent)
                    linked = independent
            if linked is not None:
                pending.append(linked)
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
    return cleanup


def raise_after_cleanup(primary: BaseException, cleanup: BaseException) -> NoReturn:
    """Keep the exact primary unless cleanup is fatal; retain links without cycles."""
    if cleanup is primary:
        raise primary
    cleanup = remove_primary_backlinks(cleanup, primary)
    if not isinstance(cleanup, Exception) or isinstance(cleanup, MemoryError):
        previous_cause = cleanup.__cause__
        if previous_cause is not None and previous_cause is not primary:
            raise cleanup from BaseExceptionGroup(
                "Fatal cleanup and primary causes.", [previous_cause, primary]
            )
        raise cleanup from primary
    previous_cause = primary.__cause__
    if previous_cause is not None and previous_cause is not cleanup:
        cleanup = BaseExceptionGroup("Primary and cleanup causes.", [previous_cause, cleanup])
    raise primary from cleanup
