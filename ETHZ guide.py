import os
import re
import shutil
from urllib.parse import urlparse

import pdfkit
import requests
from ebooklib import epub

BASE = "https://guide.acssz.org/share/glk90p035i/"
WKHTMLTOPDF_FALLBACK = r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"

os.makedirs("pages", exist_ok=True)


def parse_share_id(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "share":
        return parts[1]
    raise ValueError(f"Cannot parse shareId from URL: {url}")


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return name or "untitled"


def api_post(session: requests.Session, api_base: str, endpoint: str, payload: dict) -> dict:
    r = session.post(f"{api_base}{endpoint}", json=payload, timeout=30)
    r.raise_for_status()
    body = r.json()
    data = body.get("data")
    if data is None:
        raise RuntimeError(f"Invalid API response from {endpoint}: {body}")
    return data


def node_text(node: dict) -> str:
    t = node.get("type")
    if t == "text":
        text = node.get("text", "")
        marks = {m.get("type") for m in node.get("marks", [])}
        if "code" in marks:
            text = f"`{text}`"
        if "strong" in marks:
            text = f"**{text}**"
        if "em" in marks:
            text = f"*{text}*"
        if "strike" in marks:
            text = f"~~{text}~~"
        if "link" in marks:
            href = next((m.get("attrs", {}).get("href") for m in node.get("marks", []) if m.get("type") == "link"), "")
            if href:
                text = f"[{text}]({href})"
        return text
    return "".join(node_text(c) for c in node.get("content", []))


def render_nodes(nodes: list, indent: int = 0) -> str:
    out = []
    for n in nodes or []:
        t = n.get("type")
        if t == "paragraph":
            out.append(node_text(n))
            out.append("")
        elif t == "heading":
            level = int(n.get("attrs", {}).get("level", 1))
            level = min(max(level, 1), 6)
            out.append("#" * level + " " + node_text(n))
            out.append("")
        elif t == "bulletList":
            for item in n.get("content", []):
                out.extend(render_nodes([item], indent + 1).splitlines())
            out.append("")
        elif t == "orderedList":
            idx = 1
            for item in n.get("content", []):
                lines = render_nodes([item], indent + 1).splitlines()
                if lines:
                    out.append(("  " * indent) + f"{idx}. " + lines[0].lstrip("- "))
                    for ln in lines[1:]:
                        out.append(("  " * indent) + "   " + ln)
                idx += 1
            out.append("")
        elif t == "listItem":
            child_lines = render_nodes(n.get("content", []), indent).splitlines()
            if child_lines:
                out.append(("  " * (indent - 1)) + "- " + child_lines[0])
                for ln in child_lines[1:]:
                    out.append(("  " * indent) + ln)
        elif t == "blockquote":
            for ln in render_nodes(n.get("content", []), indent).splitlines():
                out.append("> " + ln)
            out.append("")
        elif t == "codeBlock":
            lang = n.get("attrs", {}).get("language") or ""
            code = "\n".join(node_text(c) for c in n.get("content", []))
            out.append(f"```{lang}")
            out.append(code)
            out.append("```")
            out.append("")
        elif t == "horizontalRule":
            out.append("---")
            out.append("")
        elif t == "image":
            src = n.get("attrs", {}).get("src", "")
            alt = n.get("attrs", {}).get("alt", "image")
            out.append(f"![{alt}]({src})")
            out.append("")
        elif t == "table":
            rows = n.get("content", [])
            parsed = []
            for row in rows:
                cells = [node_text(c).replace("\n", " ").strip() for c in row.get("content", [])]
                parsed.append(cells)
            if parsed:
                cols = max(len(r) for r in parsed)
                parsed = [r + [""] * (cols - len(r)) for r in parsed]
                out.append("| " + " | ".join(parsed[0]) + " |")
                out.append("| " + " | ".join(["---"] * cols) + " |")
                for r in parsed[1:]:
                    out.append("| " + " | ".join(r) + " |")
                out.append("")
        elif t == "hardBreak":
            out.append("  ")
        elif t == "doc":
            out.append(render_nodes(n.get("content", []), indent))
        else:
            txt = node_text(n)
            if txt:
                out.append(txt)
                out.append("")

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


def page_to_markdown(page: dict) -> str:
    title = page.get("title") or "untitled"
    content = page.get("content", {})
    body = render_nodes(content.get("content", []))
    return f"# {title}\n\n{body}" if body.strip() else f"# {title}\n\n"


def main():
    share_id = parse_share_id(BASE)
    api_base = f"{urlparse(BASE).scheme}://{urlparse(BASE).netloc}/api"

    session = requests.Session()

    share_info = api_post(session, api_base, "/shares/info", {"shareId": share_id})
    tree_data = api_post(session, api_base, "/shares/tree", {"shareId": share_id})

    page_tree = tree_data.get("pageTree", [])
    if not page_tree:
        raise RuntimeError("No pages found in shared tree.")

    md_files = []

    for i, p in enumerate(page_tree, start=1):
        page_id = p["id"]
        page_data = api_post(session, api_base, "/shares/page-info", {"pageId": page_id})
        page = page_data.get("page", {})
        title = page.get("title") or p.get("title") or f"page{i}"

        md = page_to_markdown(page)

        filename = f"{i:03d}_{sanitize_filename(title)}.md"
        path = os.path.join("pages", filename)
        with open(path, "w", encoding="utf8") as f:
            f.write(md)

        md_files.append(path)

    # EPUB
    book = epub.EpubBook()
    book_title = (share_info.get("sharedPage") or {}).get("title") or "Shared Guide"
    book.set_title(book_title)
    book.set_language("zh")

    chapters = []
    for i, file in enumerate(md_files, start=1):
        with open(file, encoding="utf8") as f:
            text = f.read()

        chapter = epub.EpubHtml(title=f"chapter{i}", file_name=f"chap{i}.xhtml")
        chapter.content = "<pre>" + text + "</pre>"
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = chapters
    book.spine = ["nav"] + chapters
    epub.write_epub("guide.epub", book)

    # PDF
    html_all = ""
    for file in md_files:
        with open(file, encoding="utf8") as f:
            html_all += "<pre>" + f.read() + "</pre>\n"

    with open("all.html", "w", encoding="utf8") as f:
        f.write(html_all)

    wkhtmltopdf_path = shutil.which("wkhtmltopdf") or WKHTMLTOPDF_FALLBACK
    if os.path.exists(wkhtmltopdf_path):
        config = pdfkit.configuration(wkhtmltopdf=wkhtmltopdf_path)
        pdfkit.from_file("all.html", "guide.pdf", configuration=config)
    else:
        print("guide.epub generated.")
        print("PDF skipped: wkhtmltopdf not found.")
        print("Install wkhtmltopdf or set WKHTMLTOPDF_FALLBACK path.")
        raise SystemExit(0)

    print("Done. Generated:")
    print("guide.epub")
    print("guide.pdf")
    print("pages/*.md")
    print(f"Total pages: {len(md_files)}")


if __name__ == "__main__":
    main()
