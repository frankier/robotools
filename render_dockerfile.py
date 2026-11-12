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
#   * Debian no longer ships software-properties-common, so add-apt-repository
#     is unavailable and the Ubuntu graphics PPA cannot be added. Debian
#     provides the Intel GPU packages itself, so the step is dropped.
#   * libarchive13 is named libarchive13t64 on Debian.
PPA_BLOCK_RE = re.compile(
    r"^# Repository to install Intel.*Graphics Driver\n"
    r"ARG REPOSITORY=.*\n"
    r"RUN add-apt-repository .*\n",
    re.MULTILINE,
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

    adapted, archive_count = re.subn(
        r"\blibarchive13\b", "libarchive13t64", without_spp
    )
    if archive_count != 1:
        raise SystemExit(
            "error: expected one libarchive13 in the upstream Dockerfile, "
            f"found {archive_count}; review adapt_to_debian()"
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
