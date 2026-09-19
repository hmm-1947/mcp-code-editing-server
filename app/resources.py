def check_file_growth(db: Session, user: User, growth: int, scratch: int = 0, allow_external: bool = False):
    """Preflight only; not a substitute for a filesystem quota.

    Fail closed for legacy database/image owners whose full usage cannot be
    bounded here, unless `allow_external` is set (used for the python/node
    build preflight in `require_build_headroom`, which has its own,
    coarser-grained enforcement for that case - see its docstring).
    Existing data is retained and remains deletable.
    """
    reserve(db, user)
    if not allow_external:
        has_external_storage = db.query(Site.id).filter(
            Site.owner_id == user.id, Site.container_id.isnot(None)).first() or db.query(
            UserDatabase.id).filter(UserDatabase.owner_id == user.id).first()
        if has_external_storage:
            raise HTTPException(503, "Complete storage enforcement is unavailable for this account's legacy "
                                "Docker images or shared database. A quota-backed storage backend is required before adding data.")
    try:
        used = source_usage(db, user.id)["measured_file_bytes"]
    except OSError:
        raise HTTPException(503, "Storage usage could not be measured; write was blocked.")
    allowance = limits(user.plan, db)["storage_mb"] * MIB
    if used + max(0, growth) > allowance:
        raise HTTPException(507, f"Not enough storage left on your plan. "
                            f"Used {used} bytes of {allowance}; this operation needs {max(0, growth)} additional bytes.")
    if shutil.disk_usage(SITES_DIR).free < max(0, scratch) + (
        pool(db).system_reserve_mb + pool(db).build_reserve_mb) * MIB:
        raise HTTPException(507, "Insufficient server scratch space; write was blocked.")
    return used


def require_build_headroom(db: Session, user: User, site_dir: str):
    """Preflight for a python/node Docker build (dependency install + compile).

    This cannot be a hard quota the way a plain file write can: `docker build`
    installs dependencies, runs arbitrary build scripts and writes image
    layers, and none of that can be capped to a tenant's reserved bytes from
    outside the container. What this *can* do, and does:

    1. Refuse to even start the build if the account is already at or over
       its storage allowance, or if the server doesn't have scratch space to
       spare - so an install is never attempted when it's already hopeless.
    2. Measure the site directory's size again after the build finishes
       (`build_and_run` calls `record_build_result` below). If dependency
       installation pushed the account over its allowance, the *next*
       install is blocked by step 1 until the user removes files or the
       admin adjusts the plan - the account is not rolled back or deleted
       automatically, since that would destroy a working deploy over a soft
       limit.

    In short: bounded going in, measured coming out, never silently unbounded.
    """
    return check_file_growth(db, user, 0, scratch=512 * MIB, allow_external=True)


def record_build_result(db: Session, user: User, site_dir: str) -> dict:
    """Call after a python/node build finishes (success or failure) to record
    what it actually used, so the next `require_build_headroom` call sees it.
    Never raises: a failed post-build measurement must not fail a build that
    otherwise succeeded, it just means usage_complete stays False for this
    account until the next successful measurement."""
    try:
        used = tree_bytes(site_dir)
    except OSError:
        return {"measured": False}
    allowance = limits(user.plan, db)["storage_mb"] * MIB
    return {"measured": True, "used_bytes": used, "allowance_bytes": allowance, "over_quota": used > allowance}
