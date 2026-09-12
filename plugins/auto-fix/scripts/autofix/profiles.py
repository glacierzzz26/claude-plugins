"""Model profile resolution.

The contract names profiles (``fixer = "fast"``).  A profile is resolved to an
endpoint + model + credential here, so a target repo's contract carries no
secrets and is safe to commit anywhere.

Resolution order for a profile named ``foo``:

1. ``~/.config/auto-fix/profiles.toml`` -> ``[profiles.foo]``
2. CI convention: ``AUTOFIX_PROFILE_FOO_MODEL`` / ``_BASE_URL`` / ``_TOKEN``
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from . import isolation, log
from .isolation import Profile


class ProfileError(RuntimeError):
    pass


def _load_profiles_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ProfileError(f"{path} is not valid TOML: {exc}") from exc


def _from_env(name: str) -> Profile | None:
    prefix = f"AUTOFIX_PROFILE_{name.upper()}_"
    model = os.environ.get(prefix + "MODEL")
    base_url = os.environ.get(prefix + "BASE_URL")
    if not model and not base_url:
        return None
    token = os.environ.get(prefix + "TOKEN")
    isolation.register_known_secret(token)
    return Profile(name=name, model=model or "", base_url=base_url, auth_value=token)


def resolve_profile(
    name: str,
    *,
    profiles_file_values: dict,
    env_file_values: dict[str, str],
) -> Profile:
    table = (profiles_file_values.get("profiles") or {}).get(name)
    if table:
        auth_var = table.get("auth_env_var")
        auth_value = None
        if auth_var:
            auth_value = isolation.resolve_auth(auth_var, env_file_values=env_file_values)
            if not auth_value:
                raise ProfileError(
                    f"profile {name!r} expects credential ${auth_var}, which is not set "
                    f"in the environment or in the auto-fix secrets file"
                )
        isolation.register_known_secret(auth_value)
        return Profile(
            name=name,
            model=str(table.get("model", "")),
            base_url=table.get("base_url"),
            auth_value=auth_value,
            extra_env={k: str(v) for k, v in (table.get("extra_env") or {}).items()},
            extra_args=[str(a) for a in (table.get("extra_args") or [])],
        )

    env_profile = _from_env(name)
    if env_profile is not None:
        return env_profile

    known = sorted((profiles_file_values.get("profiles") or {}).keys())
    hint = f"Known profiles: {known}" if known else "No profiles are defined yet."
    raise ProfileError(
        f"model profile {name!r} is not defined. Set [profiles.{name}] in "
        f"~/.config/auto-fix/profiles.toml or the AUTOFIX_PROFILE_{name.upper()}_* "
        f"environment variables. {hint}"
    )


def resolve_all(
    contract,
    *,
    profiles_path: Path,
    secrets_path: Path,
) -> tuple[Profile, Profile]:
    profiles_file_values = _load_profiles_file(profiles_path)
    env_file_values = isolation.parse_secrets_file(secrets_path)

    fixer = resolve_profile(
        contract.models.fixer,
        profiles_file_values=profiles_file_values,
        env_file_values=env_file_values,
    )
    reviewer = resolve_profile(
        contract.models.reviewer,
        profiles_file_values=profiles_file_values,
        env_file_values=env_file_values,
    )
    assert_distinct(fixer, reviewer, allow_self_review=contract.models.allow_self_review)
    return fixer, reviewer


def assert_distinct(fixer: Profile, reviewer: Profile, *, allow_self_review: bool) -> None:
    """Requirement 1 is 'a different model reviews'.  Enforce it, loudly.

    During bring-up before the second endpoint exists, a contract may set
    ``models.allow_self_review = true``; that downgrades the run (draft PR,
    ``needs-human``) rather than silently pretending the review is independent.
    """
    # Compare model ids, not endpoints: the requirement is that a *different
    # model* reviews.  The same id served from two URLs is still the same
    # reviewer, so keying on (model, base_url) would let that slip through.
    same = fixer.model == reviewer.model
    if same and not allow_self_review:
        raise ProfileError(
            f"fixer and reviewer resolve to the same model ({fixer.model!r}); "
            f"set models.allow_self_review = true to run in self-review mode "
            f"(draft PR + needs-human), or give them different profiles"
        )
    if same:
        log.warn(
            "self-review mode: fixer and reviewer are the same model; "
            "the run will always be marked needs-human"
        )
