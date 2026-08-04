"""Renders app.models.config_template.ConfigTemplate bodies (Jinja2
CLI/XML text with `{{ variable }}` placeholders) into a concrete
device-ready config -- "push standard access-switch template, fill in
3 variables" instead of hand-writing/pasting config from scratch on
every change request.

Deliberately a thin wrapper, not a general templating engine exposed to
the app: every render goes through a SandboxedEnvironment (blocks
attribute/method access that could reach outside the template's own
variables -- e.g. no `{{ ''.__class__.__mro__ }}` tricks) with
StrictUndefined (a missing variable is a rendering *error*, not a
silently-blank substitution in a config that's about to get pushed to a
real device).
"""
import dataclasses

import jinja2
import jinja2.meta
from jinja2.sandbox import SandboxedEnvironment


@dataclasses.dataclass
class RenderResult:
    success: bool
    rendered: str | None = None
    error: str | None = None


def _env() -> SandboxedEnvironment:
    return SandboxedEnvironment(undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True)


def extract_variable_names(template_body: str) -> list[str]:
    """Every `{{ variable }}` / `{% if variable %}` name Jinja2 can see
    referenced in the template, via static analysis (jinja2.meta) --
    doesn't require any variables to actually be supplied. Used when
    creating/editing a template to sanity-check the declared `variables`
    list actually covers what the template body references, and to
    pre-populate that list for a new template instead of the author
    having to enumerate it by hand.
    """
    env = _env()
    try:
        ast = env.parse(template_body)
    except jinja2.TemplateSyntaxError:
        return []
    return sorted(jinja2.meta.find_undeclared_variables(ast))


def render_template(template_body: str, variables: dict) -> RenderResult:
    """Renders `template_body` with `variables`. Any variable the
    template references but `variables` doesn't supply raises
    jinja2.UndefinedError (StrictUndefined) rather than rendering a
    blank/garbled config -- caught here and returned as a clear,
    actionable error instead of propagating as a raw exception.
    """
    env = _env()
    try:
        compiled = env.from_string(template_body)
        rendered = compiled.render(**variables)
        return RenderResult(success=True, rendered=rendered)
    except jinja2.UndefinedError as exc:
        return RenderResult(success=False, error=f"Missing template variable: {exc}")
    except jinja2.TemplateSyntaxError as exc:
        return RenderResult(success=False, error=f"Template syntax error: {exc}")
    except Exception as exc:  # noqa: BLE001 - never let a bad template 500 the request
        return RenderResult(success=False, error=f"Template rendering failed: {exc}")