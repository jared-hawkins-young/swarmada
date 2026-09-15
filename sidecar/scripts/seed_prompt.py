"""Seed a prompt into the Langfuse prompt registry.

Idempotent: creates or updates the named prompt at the specified version.

Usage:
    LANGFUSE_HOST=... LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... \\
        python scripts/seed_prompt.py \\
            --prompt-name drift-diagnosis \\
            --file ./prompts/drift-diagnosis.md \\
            --version v1

Or pass the body inline:
    python scripts/seed_prompt.py --prompt-name my-prompt --prompt-body "You are..."

Per Constitution Principle III (Traced By Default), all prompts live in
Langfuse and are version-pinned in the sidecar's config. This script is
the operator's tool for putting an initial prompt in place.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from langfuse import Langfuse  # type: ignore[import-not-found]


def _resolve_body(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8").strip()
    if args.prompt_body:
        return args.prompt_body.strip()
    print(
        "error: must provide either --file or --prompt-body",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--prompt-name",
        default=os.environ.get("SIDECAR_LANGFUSE_PROMPT_NAME", "drift-diagnosis"),
        help="Prompt name (Langfuse registry key). Defaults to SIDECAR_LANGFUSE_PROMPT_NAME.",
    )
    ap.add_argument(
        "--file",
        default=None,
        help="Path to a text file whose contents become the prompt body.",
    )
    ap.add_argument(
        "--prompt-body",
        default=None,
        help="Prompt body as a literal string (alternative to --file).",
    )
    ap.add_argument(
        "--version",
        default="v1",
        help="Version label applied to the seeded prompt (also labeled 'production').",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    body = _resolve_body(args)

    host = os.environ["LANGFUSE_HOST"]
    public_key = os.environ["LANGFUSE_PUBLIC_KEY"]
    secret_key = os.environ["LANGFUSE_SECRET_KEY"]

    if args.dry_run:
        print(
            f"[dry-run] would seed prompt={args.prompt_name} "
            f"version={args.version} host={host}"
        )
        print("---")
        print(body)
        return 0

    lf = Langfuse(host=host, public_key=public_key, secret_key=secret_key)
    # The pinned "version" is emitted as a label; Langfuse SDK 3.x removed
    # the `is_active` kwarg. Passing the label "production" alongside pins
    # the same prompt as the active production version.
    lf.create_prompt(
        name=args.prompt_name,
        prompt=body,
        labels=[args.version, "production"],
    )
    print(f"seeded prompt={args.prompt_name} version={args.version} at host={host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
