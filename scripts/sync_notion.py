#!/usr/bin/env python3
"""
sync_notion.py — Notion -> repo docs, one-way (ADR-0001; Notion is the source
of truth, synced docs are never hand-edited in the repo).

Project tooling, not product (ARCHITECTURE.md). Exports:
  - AGENTS.md      -> <output_dir>/AGENTS.md
  - CONTEXT.md     -> <output_dir>/CONTEXT.md
  - Specs/*        -> <output_dir>/docs/specs/<page_title>
  - ADRs/*         -> <output_dir>/docs/adr/<page_title>

Usage:
  python3 scripts/sync_notion.py

After a sync, regenerate this repo's CLAUDE.md from AGENTS.md:
  theozolith-knowledge sync --source . --scope project --target .

Requires: pip install requests
"""

import argparse
import os
import re

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

NOTION_VERSION = "2022-06-28"
HEADERS = {}

PROJECT_ROOT_PAGE_ID = (
    "3796a7adc8ed80e7bc9df7177ceb7910"  # <-- set this to your project root page ID
)


def init_headers():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise RuntimeError("Set NOTION_TOKEN environment variable")
    HEADERS.update(
        {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
        }
    )


# --- Notion API helpers ---


def get_child_pages(parent_id: str) -> list[dict]:
    """Return child pages of a block: [{"id": ..., "title": ...}, ...]"""
    pages = []
    url = f"https://api.notion.com/v1/blocks/{parent_id}/children?page_size=100"
    while url:
        r = requests.get(url, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        for block in data["results"]:
            if block["type"] == "child_page":
                pages.append(
                    {
                        "id": block["id"],
                        "title": block["child_page"]["title"],
                    }
                )
        cursor = data.get("next_cursor")
        url = (
            f"https://api.notion.com/v1/blocks/{parent_id}/children"
            f"?page_size=100&start_cursor={cursor}"
            if cursor
            else None
        )
    return pages


def get_blocks(block_id: str) -> list[dict]:
    """Recursively fetch all blocks (with children) under a block."""
    blocks = []
    url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    while url:
        r = requests.get(url, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        for block in data["results"]:
            if block.get("has_children") and block["type"] not in ("child_page", "child_database"):
                block["_children"] = get_blocks(block["id"])
            blocks.append(block)
        cursor = data.get("next_cursor")
        url = (
            f"https://api.notion.com/v1/blocks/{block_id}/children"
            f"?page_size=100&start_cursor={cursor}"
            if cursor
            else None
        )
    return blocks


# --- Rich text -> Markdown ---


def rich_text_to_md(rich_texts: list[dict]) -> str:
    """Convert Notion rich_text array to inline markdown."""
    parts = []
    for rt in rich_texts:
        text = rt.get("plain_text", "")
        ann = rt.get("annotations", {})
        href = rt.get("href")

        if ann.get("code"):
            text = f"`{text}`"
        if ann.get("bold"):
            text = f"**{text}**"
        if ann.get("italic"):
            text = f"*{text}*"
        if ann.get("strikethrough"):
            text = f"~~{text}~~"
        if href and not ann.get("code"):
            text = f"[{text}]({href})"

        parts.append(text)
    return "".join(parts)


# --- Block -> Markdown ---


def blocks_to_markdown(blocks: list[dict], indent: int = 0) -> str:
    """Convert a list of Notion blocks to a markdown string."""
    lines = []
    prefix = "  " * indent
    num_counter = 0

    for block in blocks:
        btype = block["type"]
        bdata = block.get(btype, {})

        if btype != "numbered_list_item":
            num_counter = 0

        # --- Text blocks ---

        if btype == "paragraph":
            text = rich_text_to_md(bdata.get("rich_text", []))
            lines.append(f"{prefix}{text}")
            lines.append("")

        elif btype in ("heading_1", "heading_2", "heading_3"):
            level = int(btype[-1])
            text = rich_text_to_md(bdata.get("rich_text", []))
            lines.append(f"{prefix}{'#' * level} {text}")
            lines.append("")

        elif btype == "bulleted_list_item":
            text = rich_text_to_md(bdata.get("rich_text", []))
            lines.append(f"{prefix}- {text}")
            if block.get("_children"):
                lines.append(blocks_to_markdown(block["_children"], indent + 1))

        elif btype == "numbered_list_item":
            num_counter += 1
            text = rich_text_to_md(bdata.get("rich_text", []))
            lines.append(f"{prefix}{num_counter}. {text}")
            if block.get("_children"):
                lines.append(blocks_to_markdown(block["_children"], indent + 1))

        elif btype == "to_do":
            text = rich_text_to_md(bdata.get("rich_text", []))
            checked = "x" if bdata.get("checked") else " "
            lines.append(f"{prefix}- [{checked}] {text}")
            if block.get("_children"):
                lines.append(blocks_to_markdown(block["_children"], indent + 1))

        elif btype == "code":
            text = rich_text_to_md(bdata.get("rich_text", []))
            lang = bdata.get("language", "")
            lines.append(f"{prefix}```{lang}")
            lines.append(text)
            lines.append(f"{prefix}```")
            lines.append("")

        elif btype == "quote":
            text = rich_text_to_md(bdata.get("rich_text", []))
            for line in text.split("\n"):
                lines.append(f"{prefix}> {line}")
            lines.append("")

        elif btype == "divider":
            lines.append(f"{prefix}---")
            lines.append("")

        elif btype == "table":
            lines.extend(_table_to_md(block, prefix))
            lines.append("")

        elif btype == "callout":
            text = rich_text_to_md(bdata.get("rich_text", []))
            icon = bdata.get("icon", {})
            emoji = icon.get("emoji", "") if icon.get("type") == "emoji" else ""
            lines.append(f"{prefix}> {emoji} {text}".strip())
            if block.get("_children"):
                child_md = blocks_to_markdown(block["_children"], indent)
                for line in child_md.strip().split("\n"):
                    lines.append(f"{prefix}> {line}")
            lines.append("")

        elif btype in (
            "child_page",
            "child_database",
            "embed",
            "bookmark",
            "image",
            "video",
            "file",
            "pdf",
        ):
            pass  # skip non-exportable blocks

        else:
            rt = bdata.get("rich_text")
            if rt:
                text = rich_text_to_md(rt)
                lines.append(f"{prefix}{text}")
                lines.append("")

    return "\n".join(lines)


def _table_to_md(table_block: dict, prefix: str) -> list[str]:
    """Convert a table block (with _children rows) to a markdown table."""
    rows = table_block.get("_children", [])
    if not rows:
        return []

    lines = []
    for i, row in enumerate(rows):
        cells = row.get("table_row", {}).get("cells", [])
        cell_texts = [rich_text_to_md(cell) for cell in cells]
        lines.append(f"{prefix}| {' | '.join(cell_texts)} |")
        if i == 0:
            lines.append(f"{prefix}| {' | '.join('---' for _ in cell_texts)} |")
    return lines


# --- Export logic ---


def export_page(page_id: str, filepath: str):
    """Fetch a Notion page's blocks and write as markdown."""
    blocks = get_blocks(page_id)
    md = blocks_to_markdown(blocks)

    # Clean up excessive blank lines
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"

    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"  > {filepath}")


def sync(project_root_id: str, output_dir: str):
    """Main sync: export AGENTS.md and all Specs children."""
    children = get_child_pages(project_root_id)

    # 1. Export AGENTS.md -> <output_dir>/AGENTS.md
    agents_page = next((c for c in children if c["title"] == "AGENTS.md"), None)
    if agents_page:
        export_page(agents_page["id"], os.path.join(output_dir, "AGENTS.md"))
    else:
        print("  ! No 'AGENTS.md' page found under project root")

    # 2. Export Specs/* -> <output_dir>/docs/specs/<title>
    specs_page = next((c for c in children if c["title"] == "Specs"), None)
    if specs_page:
        spec_children = get_child_pages(specs_page["id"])
        if not spec_children:
            print("  ! Specs page has no children")
        for spec in spec_children:
            filename = spec["title"]
            if not filename.endswith(".md"):
                filename += ".md"
            filepath = os.path.join(output_dir, "docs", "specs", filename)
            export_page(spec["id"], filepath)
    else:
        print("  ! No 'Specs' page found under project root")

    # 3. Export CONTEXT -> <output_dir>/CONTEXT.md
    context_page = next((c for c in children if c["title"] == "CONTEXT.md"), None)
    if context_page:
        export_page(context_page["id"], os.path.join(output_dir, "CONTEXT.md"))
    else:
        print("  ! No 'CONTEXT.md' page found under project root")

    # 4. Export ADRs/* -> <output_dir>/docs/adr/<title>
    adrs_page = next((c for c in children if c["title"] == "ADRs"), None)
    if adrs_page:
        adrs_children = get_child_pages(adrs_page["id"])
        if not adrs_children:
            print("  ! ADRs page has no children")
        for adr in adrs_children:
            filename = adr["title"]
            if not filename.endswith(".md"):
                filename += ".md"
            filepath = os.path.join(output_dir, "docs", "adr", filename)
            export_page(adr["id"], filepath)
    else:
        print("  ! No 'ADRs' page found under project root")


# --- CLI ---


def main():
    parser = argparse.ArgumentParser(description="Export Notion specs to repo")
    parser.add_argument(
        "--project-root-id",
        help="Notion page ID of the project root",
        default=PROJECT_ROOT_PAGE_ID,
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Repo root directory to write files into (default: current dir)",
    )
    args = parser.parse_args()

    init_headers()
    print(f"Syncing specs from {args.project_root_id}...")
    sync(args.project_root_id, args.output_dir)
    print("Done.")


if __name__ == "__main__":
    main()
