#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["jinja2>=3.1"]
# ///
"""Render Dockerfile.base from Dockerfile.base.tmpl plus the upstream Intel oneAPI steps.

The upstream Intel image Dockerfile is downloaded, its ``FROM`` line is removed,
and the remaining build steps are injected into the ``{{oneapi_toolkit_install}}``
placeholder of the template. The result is written next to the template with the
``.tmpl`` suffix dropped.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

import jinja2

DEFAULT_URL = (
    "https://raw.githubusercontent.com/intel/oneapi-containers/refs/heads/master"
    "/images/docker/oneapi-toolkit/Dockerfile.ubuntu-26.04"
)
DEFAULT_TEMPLATE = "Dockerfile.base.tmpl"
DEFAULT_VAR = "oneapi_toolkit_install"

FROM_RE = re.compile(r"^\s*FROM\s", re.IGNORECASE)


def fetch(url: str) -> str:
    # GitHub raw URLs need a UA; the default urllib UA is rejected by some CDNs.
    request = urllib.request.Request(url, headers={"User-Agent": "render-dockerfile"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def strip_from(text: str) -> str:
    """Remove FROM lines and normalise blank lines."""
    lines = text.splitlines()
    from_lines = [line for line in lines if FROM_RE.match(line)]
    if not from_lines:
        raise SystemExit("error: upstream Dockerfile has no FROM line")
    if len(from_lines) > 1:
        stages = ", ".join(line.strip() for line in from_lines)
        raise SystemExit(
            f"error: upstream Dockerfile is multi-stage ({stages}); "
            "copying stages and `COPY --from` instructions is not supported"
        )

    kept = [line for line in lines if not FROM_RE.match(line)]
    out: list[str] = []
    for line in kept:
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line.rstrip())
    return "\n".join(out).strip("\n") + "\n"


# The upstream snippet is written for Ubuntu 26.04, while the template's base
# image is Debian based. Three Ubuntu-only details need adapting:
#   * The Intel graphics PPA is an Ubuntu archive, so the add-apt-repository
#     step cannot work on Debian. Debian packages the Intel GPU stack itself,
#     so the step is dropped.
#   * libarchive13 was renamed to libarchive13t64 in Debian 13 (trixie) and
#     Ubuntu 24.04, while Debian 12 (bookworm) - the release the Paseo base
#     image is built on - still ships libarchive13.
#   * libze-intel-gpu1 (the Level Zero GPU driver) is packaged by Debian 13 and
#     by the Ubuntu graphics PPA, but neither Debian 12 nor Intel's oneAPI
#     repository ships it, so it is unavailable on bookworm.
# Both package names are resolved at build time instead of being hardcoded to a
# single Debian release, so the rendered Dockerfile keeps building when the base
# image moves to a newer Debian.
PPA_BLOCK_RE = re.compile(
    r"^# Repository to install Intel.*Graphics Driver\n"
    r"ARG REPOSITORY=.*\n"
    r"RUN add-apt-repository .*\n",
    re.MULTILINE,
)


def apt_pick(*names: str) -> str:
    """Return a shell expression that expands to the first package apt knows.

    The snippet's install steps run ``apt-get update`` before installing, so
    ``apt-cache`` can answer by the time this expression is evaluated. It
    expands to nothing when none of the names is available.
    """
    probes = " ".join(names)
    return (
        f'$(for p in {probes}; do '
        'apt-cache show "$p" >/dev/null 2>&1 && { echo "$p"; break; }; done)'
    )


def adapt_to_debian(snippet: str) -> str:
    """Rewrite the Ubuntu-only parts of the upstream snippet for Debian."""
    without_ppa, ppa_count = PPA_BLOCK_RE.subn("", snippet)
    if ppa_count != 1:
        raise SystemExit(
            "error: expected one Intel graphics PPA step in the upstream "
            f"Dockerfile, found {ppa_count}; review adapt_to_debian()"
        )

    without_spp, spp_count = re.subn(
        r"\s+software-properties-common", "", without_ppa
    )
    if spp_count != 1:
        raise SystemExit(
            "error: expected one software-properties-common in the upstream "
            f"Dockerfile, found {spp_count}; review adapt_to_debian()"
        )

    with_archive, archive_count = re.subn(
        r"\blibarchive13\b", apt_pick("libarchive13t64", "libarchive13"), without_spp
    )
    if archive_count != 1:
        raise SystemExit(
            "error: expected one libarchive13 in the upstream Dockerfile, "
            f"found {archive_count}; review adapt_to_debian()"
        )

    adapted, gpu_count = re.subn(
        r"\blibze-intel-gpu1\b", apt_pick("libze-intel-gpu1"), with_archive
    )
    if gpu_count != 1:
        raise SystemExit(
            "error: expected one libze-intel-gpu1 in the upstream Dockerfile, "
            f"found {gpu_count}; review adapt_to_debian()"
        )
    return adapted


def render(template_path: Path, var: str, snippet: str) -> str:
    source = template_path.read_text(encoding="utf-8")
    env = jinja2.Environment(
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
        autoescape=False,
    )
    rendered = env.from_string(source).render({var: snippet})
    rendered = rendered.rstrip("\n") + "\n"

    from_count = len([ln for ln in rendered.splitlines() if FROM_RE.match(ln)])
    if from_count != 1:
        raise SystemExit(f"error: rendered output has {from_count} FROM lines, expected 1")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="upstream Dockerfile URL")
    parser.add_argument(
        "--template",
        type=Path,
        default=Path(__file__).with_name(DEFAULT_TEMPLATE),
        help=f"template to render (default: %(default)s)",
    )
    parser.add_argument(
        "--variable",
        default=DEFAULT_VAR,
        help=f"template variable for the upstream steps (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="output path (default: template path with .tmpl removed)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if the output would change",
    )
    args = parser.parse_args(argv)

    output = args.output or args.template.with_suffix("")
    snippet = adapt_to_debian(strip_from(fetch(args.url)))
    content = render(args.template, args.variable, snippet)

    if args.check:
        current = output.read_text(encoding="utf-8") if output.exists() else ""
        if current != content:
            print(f"{output} is out of date", file=sys.stderr)
            return 1
        print(f"{output} is up to date")
        return 0

    output.write_text(content, encoding="utf-8")
    print(f"wrote {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
