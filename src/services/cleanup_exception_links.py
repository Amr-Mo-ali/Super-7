"""Remove cleanup backlinks before joining an active failure to shutdown errors."""

from __future__ import annotations


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
