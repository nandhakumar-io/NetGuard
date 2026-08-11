"""Config-as-code / GitOps sync for app.models.config_template.ConfigTemplate.

Design
------
Each app.models.git_repo_config.GitRepoConfig row points at one Git
repository. Working copies live under WORKDIR_ROOT/<repo id>, cloned
shallow (`--depth 1`) and kept as a bare checkout of a single branch --
this is a sync mechanism, not a general-purpose Git client, so there's no
need for history.

File format: one file per template under `template_path`, YAML
front-matter + body:

    ---
    name: standard-access-switch
    device_role: access
    vendor: cisco
    ---
    interface {{ interface_name }}
     switchport mode access
     switchport access vlan {{ vlan_id }}

PULL direction: `sync_repo` reads every matching file, and for each one
either creates a new ConfigTemplate (first time `name` is seen) or -- if
the file's body/variables differ from the template's current published
version -- submits a new PENDING_APPROVAL ConfigTemplateVersion, exactly
like a human clicking "Submit for review" in the UI. It never
auto-publishes: a Network Admin still has to approve the version in
NetGuard, same segregation-of-duties guarantee as any other template
change. This is what makes it "PR-triggered change requests" in
practice -- merging a PR to the watched branch fires the webhook, which
queues a review in NetGuard rather than silently going live.

PUSH direction: `push_template_version` (called from
app.api.config_templates.approve_template_version right after a version
is published) writes that version's body back to its file in the repo,
commits, and pushes -- so the repo mirrors exactly what's live in
NetGuard.

Git operations shell out to the `git` binary via subprocess rather than
pulling in a Git library dependency -- every operation used here (clone,
pull, add, commit, push) is a single, well-understood CLI call, and
subprocess keeps the sandboxing (working directory, timeout, env) explicit.
"""
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.core import crypto
from app.models.config_template import (
    ConfigTemplate,
    ConfigTemplateVersion,
    TemplateVersionStatus,
)
from app.models.git_repo_config import GitRepoConfig, GitSyncStatus

logger = logging.getLogger(__name__)

WORKDIR_ROOT = Path(tempfile.gettempdir()) / "netguard-gitops"
GIT_TIMEOUT_SECONDS = 60
FRONT_MATTER_RE = re.compile(r"^---\s*\n(?P<meta>.*?)\n---\s*\n(?P<body>.*)$", re.DOTALL)


class GitSyncError(Exception):
    pass


# --- Low-level git plumbing -------------------------------------------------


def _authed_url(repo_url: str, token: str | None) -> str:
    """Injects an access token into an https:// remote URL as
    `x-access-token:<token>@host` (works for GitHub/GitLab/Bitbucket App
    Password style tokens). Left untouched for ssh:// URLs -- those
    authenticate via the host's own SSH agent/keys, not a token."""
    if not token or not repo_url.startswith("https://"):
        return repo_url
    parts = urlsplit(repo_url)
    netloc = f"x-access-token:{token}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        # stderr may contain the auth-embedded URL on some git versions'
        # error output -- strip anything that looks like a credential
        # before it ever reaches a log line or an API error response.
        safe_err = re.sub(r"://[^@/]+@", "://***@", exc.stderr or "")
        raise GitSyncError(f"git {' '.join(args[:1])} failed: {safe_err.strip()[:500]}")
    except subprocess.TimeoutExpired:
        raise GitSyncError(f"git {' '.join(args[:1])} timed out after {GIT_TIMEOUT_SECONDS}s")


def _working_dir(repo: GitRepoConfig) -> Path:
    return WORKDIR_ROOT / str(repo.id)


def _decrypt_token(repo: GitRepoConfig) -> str | None:
    return crypto.decrypt(repo.access_token_encrypted) if repo.access_token_encrypted else None


def _ensure_clone(repo: GitRepoConfig) -> Path:
    """Clones the repo (shallow, single branch) if it isn't checked out
    yet, otherwise fetches+resets to the latest remote commit. Resetting
    rather than merging keeps this a one-way mirror of the remote branch
    -- NetGuard never has local commits sitting in this working copy
    except the ones push_template_version is about to make and push
    immediately."""
    token = _decrypt_token(repo)
    remote = _authed_url(repo.repo_url, token)
    work_dir = _working_dir(repo)

    if (work_dir / ".git").exists():
        _run_git(["remote", "set-url", "origin", remote], cwd=work_dir)
        _run_git(["fetch", "--depth", "1", "origin", repo.branch], cwd=work_dir)
        _run_git(["reset", "--hard", f"origin/{repo.branch}"], cwd=work_dir)
    else:
        work_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["clone", "--depth", "1", "--branch", repo.branch, "--single-branch", remote, str(work_dir)]
        )
        _run_git(["config", "user.email", "gitops@netguard.local"], cwd=work_dir)
        _run_git(["config", "user.name", "NetGuard GitOps"], cwd=work_dir)

    return work_dir


def _current_commit(work_dir: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], cwd=work_dir)


# --- Front-matter parsing ---------------------------------------------------


def _parse_front_matter(text: str) -> tuple[dict, str] | None:
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return None
    meta: dict = {}
    for line in match.group("meta").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, match.group("body")


def _slug_filename(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{slug}.j2"


# --- Pull: repo -> ConfigTemplate versions ----------------------------------


def sync_repo(db: Session, repo: GitRepoConfig) -> dict:
    """Pulls the repo and, for every template file under
    `template_path`, ensures a ConfigTemplate exists and (if the file's
    content differs from what's currently published) submits a new
    PENDING_APPROVAL version. Returns a small summary dict for the
    caller to log/report; never raises GitSyncError to the caller --
    catches it and records it on the repo row instead, same
    fail-safe pattern as notification_service.
    """
    repo.last_sync_status = GitSyncStatus.SYNCING
    db.commit()

    created, updated, unchanged, errors = 0, 0, 0, []
    try:
        work_dir = _ensure_clone(repo)
        template_dir = work_dir / repo.template_path
        if not template_dir.exists():
            raise GitSyncError(f"template_path '{repo.template_path}' does not exist in the repo")

        for path in sorted(template_dir.rglob("*")):
            if not path.is_file() or path.suffix not in (".j2", ".yaml", ".yml", ".txt", ".cfg"):
                continue
            try:
                outcome = _sync_one_file(db, path)
                if outcome == "created":
                    created += 1
                elif outcome == "updated":
                    updated += 1
                else:
                    unchanged += 1
            except GitSyncError as exc:
                errors.append(f"{path.name}: {exc}")

        repo.last_synced_commit = _current_commit(work_dir)
        repo.last_sync_status = GitSyncStatus.FAILED if errors else GitSyncStatus.SUCCEEDED
        repo.last_sync_error = "; ".join(errors) if errors else None
    except GitSyncError as exc:
        repo.last_sync_status = GitSyncStatus.FAILED
        repo.last_sync_error = str(exc)
        errors.append(str(exc))
    except Exception as exc:  # noqa: BLE001 -- never let a sync bug crash the caller/task
        logger.exception("Unexpected error syncing GitRepoConfig %s", repo.id)
        repo.last_sync_status = GitSyncStatus.FAILED
        repo.last_sync_error = f"Unexpected error: {exc}"[:500]
        errors.append(repo.last_sync_error)
    finally:
        import datetime

        repo.last_synced_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()

    return {"created": created, "updated": updated, "unchanged": unchanged, "errors": errors}


def _sync_one_file(db: Session, path: Path) -> str:
    parsed = _parse_front_matter(path.read_text(encoding="utf-8", errors="replace"))
    if not parsed:
        raise GitSyncError("missing '---' front-matter header (name/device_role/vendor)")
    meta, body = parsed
    name = meta.get("name")
    if not name:
        raise GitSyncError("front-matter is missing required 'name' field")
    body = body.strip("\n") + "\n"

    template = db.query(ConfigTemplate).filter(ConfigTemplate.name == name).first()
    if not template:
        template = ConfigTemplate(
            name=name, device_role=meta.get("device_role") or None, vendor=meta.get("vendor") or None,
            body=body, created_by="gitops-sync",
        )
        db.add(template)
        db.commit()
        db.refresh(template)
        _submit_version(db, template, body, change_note="Initial import from Git")
        return "created"

    published = (
        db.get(ConfigTemplateVersion, template.published_version_id)
        if template.published_version_id else None
    )
    if published and published.body.strip() == body.strip():
        return "unchanged"

    template.body = body
    template.device_role = meta.get("device_role") or template.device_role
    template.vendor = meta.get("vendor") or template.vendor
    db.commit()
    _submit_version(db, template, body, change_note="Updated from Git")
    return "updated"


def _submit_version(db: Session, template: ConfigTemplate, body: str, change_note: str) -> ConfigTemplateVersion:
    last = (
        db.query(ConfigTemplateVersion)
        .filter(ConfigTemplateVersion.template_id == template.id)
        .order_by(ConfigTemplateVersion.version_number.desc())
        .first()
    )
    version = ConfigTemplateVersion(
        template_id=template.id, version_number=(last.version_number + 1) if last else 1,
        body=body, variables=template.variables,
        status=TemplateVersionStatus.PENDING_APPROVAL.value,
        change_note=change_note, submitted_by="gitops-sync",
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


# --- Push: published ConfigTemplateVersion -> repo --------------------------


def push_template_version(db: Session, repo: GitRepoConfig, template: ConfigTemplate, version: ConfigTemplateVersion) -> None:
    """Commits+pushes a just-published version to the repo. Best-effort:
    raises GitSyncError on failure so the caller (approve_template_version)
    can log it without blocking the approval itself -- the approval in
    NetGuard is the source of truth regardless of whether the mirror
    commit succeeds."""
    work_dir = _ensure_clone(repo)
    template_dir = work_dir / repo.template_path
    template_dir.mkdir(parents=True, exist_ok=True)
    file_path = template_dir / _slug_filename(template.name)

    front_matter = (
        f"---\nname: {template.name}\n"
        f"device_role: {template.device_role or ''}\n"
        f"vendor: {template.vendor or ''}\n---\n"
    )
    file_path.write_text(front_matter + version.body.strip("\n") + "\n", encoding="utf-8")

    _run_git(["add", str(file_path.relative_to(work_dir))], cwd=work_dir)
    status = _run_git(["status", "--porcelain"], cwd=work_dir)
    if not status:
        return  # nothing changed (repo already matched this version)

    _run_git(
        ["commit", "-m", f"NetGuard: publish {template.name} v{version.version_number}"],
        cwd=work_dir,
    )
    _run_git(["push", "origin", f"HEAD:{repo.branch}"], cwd=work_dir)
    repo.last_synced_commit = _current_commit(work_dir)


# --- Inbound webhook verification -------------------------------------------


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Verifies GitHub's `X-Hub-Signature-256: sha256=<hmac>` header."""
    import hashlib
    import hmac as hmac_mod

    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac_mod.compare_digest(expected, signature_header)
