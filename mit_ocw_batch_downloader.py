"""A small Windows-friendly GUI for downloading MIT OpenCourseWare course files."""

from __future__ import annotations

import html.parser
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "MIT OCW 批量下载器"
USER_AGENT = "MIT-OCW-Batch-Downloader/1.1 (personal educational use)"
ALLOWED_HOSTS = {"ocw.mit.edu", "www.ocw.mit.edu"}
DOWNLOAD_EXTENSIONS = {
    ".7z", ".aac", ".c", ".cpp", ".csv", ".doc", ".docx", ".gif", ".gz",
    ".html", ".ipynb", ".java", ".jpeg", ".jpg", ".js", ".json", ".m", ".mat",
    ".m4a", ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg", ".mpg", ".ogg",
    ".pdf", ".png", ".ppt", ".pptx", ".py", ".rtf", ".svg", ".tar", ".tex",
    ".txt", ".wav", ".webm", ".xls", ".xlsx", ".xml", ".zip",
}
CHUNK_SIZE = 128 * 1024
CATEGORY_LABELS = {
    "assignments": "作业 / Assignments",
    "exams": "考试 / Exams",
    "exams-with-solutions": "含答案的考试 / Exams with Solutions",
    "exercises": "练习题 / Exercises",
    "finger-exercises": "练习题 / Finger Exercises",
    "homework": "作业 / Homework",
    "labs": "实验 / Labs",
    "lecture-code": "课程代码 / Lecture Code",
    "lecture-notes": "课程讲义 / Lecture Notes",
    "lecture-videos": "课程视频 / Lecture Videos",
    "problem-set-solutions": "习题答案 / Problem Set Solutions",
    "problem-sets": "习题集 / Problem Sets",
    "problem-sets-with-solutions": "含答案的习题集 / Problem Sets with Solutions",
    "projects": "课程项目 / Projects",
    "quizzes": "测验 / Quizzes",
    "quizzes-with-solutions": "含答案的测验 / Quizzes with Solutions",
    "readings": "阅读材料 / Readings",
    "recitations": "习题课 / Recitations",
    "study-materials": "学习资料 / Study Materials",
    "tutorials": "教程 / Tutorials",
}


class OcwRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not is_allowed_host(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "Redirect to a non-MIT OCW host is not allowed", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


HTTP_OPENER = urllib.request.build_opener(OcwRedirectHandler())


@dataclass
class Link:
    href: str
    text: str


@dataclass
class DownloadItem:
    url: str
    label: str


@dataclass
class ResourceCategory:
    slug: str
    label: str
    url: str


class AnchorParser(html.parser.HTMLParser):
    """Collect links without depending on BeautifulSoup or page-specific markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: List[Link] = []
        self._current: Optional[Dict[str, object]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a":
            attributes = dict(attrs)
            self._current = {"href": attributes.get("href") or "", "text": []}
        elif tag.lower() == "img" and self._current is not None:
            attributes = dict(attrs)
            alt = attributes.get("alt") or ""
            if alt:
                self._current["text"].append(alt)  # type: ignore[union-attr]

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"].append(data)  # type: ignore[union-attr]

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current is not None:
            href = str(self._current["href"]).strip()
            text = " ".join(" ".join(self._current["text"]).split())  # type: ignore[arg-type]
            if href:
                self.links.append(Link(href=href, text=text))
            self._current = None


def is_allowed_host(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ALLOWED_HOSTS


def course_details(raw_url: str) -> Tuple[str, str, str]:
    value = raw_url.strip()
    if not value:
        raise ValueError("课程链接不能为空。")
    if "://" not in value:
        value = "https://" + value

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not is_allowed_host(value):
        raise ValueError("只支持 MIT OpenCourseWare 的 ocw.mit.edu 课程链接。")

    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    match = re.match(r"^(/courses/[^/]+/)", path)
    if not match:
        raise ValueError("请粘贴课程主页、课程资源页或课程下载页链接。")

    course_path = match.group(1)
    course_slug = course_path.strip("/").split("/")[-1]
    course_base = urllib.parse.urlunparse(("https", "ocw.mit.edu", course_path, "", "", ""))
    return course_base, course_slug, course_base + "download/"


def fetch_links(page_url: str, cancel_event: threading.Event) -> List[Link]:
    if not is_allowed_host(page_url):
        return []
    request = urllib.request.Request(page_url, headers={"User-Agent": USER_AGENT})
    with HTTP_OPENER.open(request, timeout=30) as response:
        final_url = response.geturl()
        if not is_allowed_host(final_url):
            raise ValueError("MIT OCW 页面跳转到了非 MIT OCW 域名，已停止读取。")
        charset = response.headers.get_content_charset() or "utf-8"
        chunks: List[bytes] = []
        while True:
            if cancel_event.is_set():
                return []
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        source = b"".join(chunks).decode(charset, errors="replace")

    parser = AnchorParser()
    parser.feed(source)
    parser.close()
    return parser.links


def normalized_link(page_url: str, href: str) -> str:
    absolute = urllib.parse.urljoin(page_url, href.strip())
    parts = urllib.parse.urlsplit(absolute)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def is_course_link(url: str, course_base: str) -> bool:
    if not is_allowed_host(url):
        return False
    return urllib.parse.urlparse(url).path.startswith(urllib.parse.urlparse(course_base).path)


def file_extension(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    return PurePosixPath(path.lower()).suffix


def category_from_link(link: Link, absolute_url: str, course_base: str) -> Optional[ResourceCategory]:
    course_path = urllib.parse.urlparse(course_base).path
    path = urllib.parse.urlparse(absolute_url).path
    resource_prefix = course_path + "resources/"
    if not path.startswith(resource_prefix):
        return None
    remainder = path[len(resource_prefix):].strip("/")
    if not remainder:
        return None
    slug = remainder.split("/")[0]
    if file_extension(absolute_url) in DOWNLOAD_EXTENSIONS:
        return None
    label = CATEGORY_LABELS.get(slug, " / ".join((slug.replace("-", " ").title(), slug)))
    return ResourceCategory(slug=slug, label=label, url=absolute_url)


def discover_course_structure(
    course_base: str,
    download_page: str,
    cancel_event: threading.Event,
    status: Callable[[str], None],
) -> Tuple[List[ResourceCategory], Optional[str]]:
    status("正在扫描课程资源类型：" + download_page)
    links = fetch_links(download_page, cancel_event)
    categories: Dict[str, ResourceCategory] = {}
    package_url: Optional[str] = None

    for link in links:
        absolute = normalized_link(download_page, link.href)
        if not is_course_link(absolute, course_base):
            continue
        if "download course" in link.text.casefold() and file_extension(absolute) == ".zip":
            package_url = absolute
        category = category_from_link(link, absolute, course_base)
        if category is not None:
            categories.setdefault(category.slug, category)

    return sorted(categories.values(), key=lambda item: item.label.casefold()), package_url


def collect_category_files(
    category: ResourceCategory,
    course_base: str,
    cancel_event: threading.Event,
) -> List[DownloadItem]:
    items: Dict[str, DownloadItem] = {}
    for link in fetch_links(category.url, cancel_event):
        absolute = normalized_link(category.url, link.href)
        if not is_course_link(absolute, course_base):
            continue
        if file_extension(absolute) not in DOWNLOAD_EXTENSIONS:
            continue
        name = link.text or PurePosixPath(urllib.parse.urlparse(absolute).path).name
        items.setdefault(absolute, DownloadItem(absolute, "{}：{}".format(category.label, name)))
    return list(items.values())


def safe_relative_path(url: str, course_base: str) -> Path:
    url_path = urllib.parse.urlparse(url).path
    prefix = urllib.parse.urlparse(course_base).path
    relative = urllib.parse.unquote(url_path[len(prefix):]) if url_path.startswith(prefix) else ""
    posix_path = PurePosixPath(relative)
    parts = [part for part in posix_path.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        parts = [PurePosixPath(url_path).name or "download.bin"]
    cleaned = []
    for part in parts:
        safe_part = re.sub(r'[<>:"|?*\x00-\x1f]', "_", part).rstrip(" .")
        cleaned.append(safe_part or "download.bin")
    return Path(*cleaned)


class DownloadCancelled(Exception):
    pass


def download_one(
    item: DownloadItem,
    destination: Path,
    course_base: str,
    cancel_event: threading.Event,
    progress: Callable[[int, Optional[int]], None],
    status: Callable[[str], None],
) -> str:
    target = destination / safe_relative_path(item.url, course_base)
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(item.url, headers={"User-Agent": USER_AGENT})

    with HTTP_OPENER.open(request, timeout=45) as response:
        if not is_allowed_host(response.geturl()):
            raise ValueError("下载地址跳转到了非 MIT OCW 域名。")
        length_header = response.headers.get("Content-Length")
        total = int(length_header) if length_header and length_header.isdigit() else None
        if target.is_file() and total is not None and target.stat().st_size == total:
            status("已存在并跳过：" + str(target.name))
            return "skipped"

        temporary = target.with_name(target.name + ".part")
        received = 0
        last_report = 0.0
        try:
            with temporary.open("wb") as output:
                while True:
                    if cancel_event.is_set():
                        raise DownloadCancelled()
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
                    now = time.monotonic()
                    if now - last_report >= 0.25 or (total is not None and received >= total):
                        progress(received, total)
                        last_report = now
            if total is not None and received != total:
                raise IOError("下载长度与服务器提供的文件长度不一致。")
            temporary.replace(target)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    progress(received, total)
    return "downloaded"


class DownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("850x720")
        self.root.minsize(740, 630)

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.scanned_courses: List[dict] = []
        self.scanned_urls: List[str] = []
        self.category_slugs: List[str] = []

        self.output_dir = tk.StringVar(value=str(Path.home() / "Downloads" / "MIT-OCW"))
        self.include_course_package = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="粘贴课程链接，扫描资源类型后选择要下载的类别。")

        self._build_ui()
        self.root.after(150, self._poll_events)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        frame.rowconfigure(7, weight=1)
        frame.rowconfigure(12, weight=1)

        ttk.Label(frame, text="MIT OCW 课程链接", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="每行一个课程主页或资源页链接，可一次粘贴多门课程。", foreground="#555").grid(row=1, column=0, sticky="w", pady=(3, 6))

        self.url_box = tk.Text(frame, height=5, wrap="word", undo=True)
        self.url_box.grid(row=2, column=0, sticky="nsew")
        self.url_box.insert("1.0", "https://ocw.mit.edu/courses/6-100l-introduction-to-cs-and-programming-using-python-fall-2022/")

        ttk.Label(frame, text="保存位置", font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="w", pady=(14, 4))
        folder_row = ttk.Frame(frame)
        folder_row.grid(row=4, column=0, sticky="ew")
        folder_row.columnconfigure(0, weight=1)
        ttk.Entry(folder_row, textvariable=self.output_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder_row, text="浏览…", command=self._choose_folder).grid(row=0, column=1, padx=(8, 0))

        controls = ttk.Frame(frame)
        controls.grid(row=5, column=0, sticky="ew", pady=(10, 6))
        self.scan_button = ttk.Button(controls, text="扫描资源类型", command=self._scan)
        self.scan_button.pack(side="left")
        ttk.Label(controls, text="先扫描，再选择需要下载的类别。", foreground="#555").pack(side="left", padx=(10, 0))

        ttk.Label(frame, text="资源类型（按住 Ctrl 或 Shift 可多选）", font=("Segoe UI", 10, "bold")).grid(row=6, column=0, sticky="w", pady=(5, 4))
        category_frame = ttk.Frame(frame)
        category_frame.grid(row=7, column=0, sticky="nsew")
        category_frame.rowconfigure(0, weight=1)
        category_frame.columnconfigure(0, weight=1)
        self.category_list = tk.Listbox(category_frame, selectmode="extended", exportselection=False, height=6)
        self.category_list.grid(row=0, column=0, sticky="nsew")
        category_scrollbar = ttk.Scrollbar(category_frame, orient="vertical", command=self.category_list.yview)
        category_scrollbar.grid(row=0, column=1, sticky="ns")
        self.category_list.configure(yscrollcommand=category_scrollbar.set)

        options = ttk.Frame(frame)
        options.grid(row=8, column=0, sticky="ew", pady=(5, 4))
        ttk.Button(options, text="全选类型", command=self._select_all_categories).pack(side="left")
        ttk.Button(options, text="清空选择", command=self._clear_categories).pack(side="left", padx=(6, 12))
        ttk.Checkbutton(
            options,
            text="额外下载整门课程 ZIP（包含所有资源类型）",
            variable=self.include_course_package,
        ).pack(side="left")

        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, sticky="ew", pady=(5, 6))
        self.start_button = ttk.Button(buttons, text="下载所选类型", command=self._start, state="disabled")
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=1, value=0)
        self.progress.grid(row=10, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(frame, textvariable=self.status_text, foreground="#333").grid(row=11, column=0, sticky="nw", pady=(2, 5))

        log_frame = ttk.LabelFrame(frame, text="下载记录", padding=6)
        log_frame.grid(row=12, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        ttk.Label(frame, text="只下载选中分类页面内的资源；整门课程 ZIP 是单独选项。", foreground="#666").grid(row=13, column=0, sticky="w", pady=(9, 0))

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.home()))
        if selected:
            self.output_dir.set(selected)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _read_urls(self) -> List[str]:
        raw_urls = self.url_box.get("1.0", "end").splitlines()
        return [line.strip() for line in raw_urls if line.strip()]

    def _scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        urls = self._read_urls()
        if not urls:
            messagebox.showinfo(APP_TITLE, "请先粘贴至少一个 MIT OCW 课程链接。")
            return

        self.cancel_event.clear()
        self.progress.configure(maximum=1, value=0)
        self.status_text.set("正在扫描课程资源类型…")
        self.scanned_courses = []
        self.scanned_urls = urls
        self.category_slugs = []
        self.category_list.delete(0, "end")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.scan_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.worker = threading.Thread(
            target=self._run_scan,
            args=(urls,),
            daemon=True,
        )
        self.worker.start()

    def _run_scan(self, urls: List[str]) -> None:
        records = []
        failures = []
        for raw_url in urls:
            if self.cancel_event.is_set():
                break
            try:
                course_base, slug, download_page = course_details(raw_url)
                categories, package_url = discover_course_structure(
                    course_base,
                    download_page,
                    self.cancel_event,
                    lambda message: self.events.put(("log", message)),
                )
                records.append({
                    "raw_url": raw_url,
                    "course_base": course_base,
                    "slug": slug,
                    "download_page": download_page,
                    "categories": categories,
                    "package_url": package_url,
                })
                self.events.put(("log", "{}：发现 {} 种资源类型{}".format(
                    slug,
                    len(categories),
                    "，并找到整门课程 ZIP" if package_url else "",
                )))
            except Exception as exc:
                failures.append("{} ({})".format(raw_url, exc))
                self.events.put(("log", "扫描失败：{} — {}".format(raw_url, exc)))
        self.events.put(("scan_done", records, failures, self.cancel_event.is_set()))

    def _select_all_categories(self) -> None:
        if self.category_slugs:
            self.category_list.selection_set(0, "end")

    def _clear_categories(self) -> None:
        self.category_list.selection_clear(0, "end")

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        urls = self._read_urls()
        if not urls:
            messagebox.showinfo(APP_TITLE, "请先粘贴至少一个 MIT OCW 课程链接。")
            return
        if urls != self.scanned_urls or not self.scanned_courses:
            messagebox.showinfo(APP_TITLE, "请先扫描当前课程链接的资源类型。")
            return
        selected_indices = self.category_list.curselection()
        selected_slugs = [self.category_slugs[index] for index in selected_indices]
        include_package = self.include_course_package.get()
        if not selected_slugs and not include_package:
            messagebox.showinfo(APP_TITLE, "请至少选择一种资源类型，或勾选整门课程 ZIP。")
            return
        output = self.output_dir.get().strip()
        if not output:
            messagebox.showinfo(APP_TITLE, "请选择保存位置。")
            return

        self.cancel_event.clear()
        self.progress.configure(maximum=1, value=0)
        self.status_text.set("正在准备所选资源…")
        self.scan_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(self.scanned_courses, selected_slugs, include_package, Path(output)),
            daemon=True,
        )
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status_text.set("正在停止；当前文件会保留为未完成状态并清理临时文件。")
        self.cancel_button.configure(state="disabled")

    def _run_batch(
        self,
        courses: List[dict],
        selected_slugs: List[str],
        include_package: bool,
        output_root: Path,
    ) -> None:
        totals = {"downloaded": 0, "skipped": 0, "failed": 0, "courses": 0}
        failures: List[str] = []
        try:
            output_root.mkdir(parents=True, exist_ok=True)
            for course in courses:
                if self.cancel_event.is_set():
                    break
                slug = course["slug"]
                course_base = course["course_base"]
                destination = output_root / re.sub(r'[<>:"|?*\x00-\x1f]', "_", slug)
                try:
                    self.events.put(("status", "正在处理课程：" + slug))
                    items: Dict[str, DownloadItem] = {}
                    categories = {category.slug: category for category in course["categories"]}
                    for category_slug in selected_slugs:
                        if self.cancel_event.is_set():
                            break
                        category = categories.get(category_slug)
                        if category is None:
                            continue
                        self.events.put(("status", "正在读取 {}：{}".format(slug, category.label)))
                        category_items = collect_category_files(category, course_base, self.cancel_event)
                        for item in category_items:
                            items.setdefault(item.url, item)

                    if include_package and course["package_url"]:
                        package_url = course["package_url"]
                        items.setdefault(package_url, DownloadItem(package_url, "课程完整资料包（包含所有类型）"))
                    elif include_package:
                        self.events.put(("log", "{}：没有提供整门课程 ZIP，跳过该选项。".format(slug)))

                    if self.cancel_event.is_set():
                        break
                    if not items:
                        self.events.put(("log", "{}：所选类型没有找到可下载文件。".format(slug)))
                        totals["courses"] += 1
                        continue
                    self.events.put(("course_items", slug, len(items), str(destination)))
                    for index, item in enumerate(items.values(), start=1):
                        if self.cancel_event.is_set():
                            break
                        self.events.put(("status", "{}：{}/{}  {}".format(slug, index, len(items), item.label)))
                        self.events.put(("file_progress", index - 1, len(items), 0, None))
                        try:
                            result = download_one(
                                item,
                                destination,
                                course_base,
                                self.cancel_event,
                                lambda received, total, i=index, n=len(items): self.events.put(("file_progress", i - 1, n, received, total)),
                                lambda message: self.events.put(("log", message)),
                            )
                            totals[result] += 1
                            self.events.put(("log", "完成：" + item.label))
                            self.events.put(("file_progress", index, len(items), 0, None))
                        except DownloadCancelled:
                            break
                        except Exception as exc:
                            totals["failed"] += 1
                            failures.append("{}：{} ({})".format(slug, item.label, exc))
                            self.events.put(("log", "失败：{} — {}".format(item.label, exc)))
                    totals["courses"] += 1
                except Exception as exc:
                    totals["failed"] += 1
                    failures.append("{} ({})".format(slug, exc))
                    self.events.put(("log", "课程处理失败：{} — {}".format(slug, exc)))
        except Exception as exc:
            failures.append(str(exc))
            totals["failed"] += 1
            self.events.put(("log", "任务失败：" + str(exc)))

        was_cancelled = self.cancel_event.is_set()
        self.events.put(("done", totals, failures, was_cancelled))

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "status":
                    self.status_text.set(event[1])
                elif kind == "log":
                    self._append_log(event[1])
                elif kind == "course_items":
                    _, slug, count, destination = event
                    self.progress.configure(maximum=count, value=0)
                    self._append_log("{}：找到 {} 个文件，保存到 {}".format(slug, count, destination))
                elif kind == "file_progress":
                    _, complete, total_files, received, total_bytes = event
                    self.progress.configure(maximum=max(total_files, 1), value=complete)
                    if total_bytes:
                        self.status_text.set("当前文件：{:.1f} / {:.1f} MB".format(received / 1048576, total_bytes / 1048576))
                elif kind == "scan_done":
                    _, records, failures, was_cancelled = event
                    self.scanned_courses = records
                    categories: Dict[str, ResourceCategory] = {}
                    for course in records:
                        for category in course["categories"]:
                            categories.setdefault(category.slug, category)
                    ordered = sorted(categories.values(), key=lambda item: item.label.casefold())
                    self.category_slugs = [category.slug for category in ordered]
                    self.category_list.delete(0, "end")
                    for category in ordered:
                        self.category_list.insert("end", "{}  [{}]".format(category.label, category.slug))
                    self.scan_button.configure(state="normal")
                    self.start_button.configure(state="normal" if records else "disabled")
                    self.cancel_button.configure(state="disabled")
                    summary = "扫描完成：{} 门课程，发现 {} 种资源类型。".format(len(records), len(ordered))
                    if was_cancelled:
                        summary = "扫描已取消。" + summary
                    self.status_text.set(summary)
                    self._append_log(summary)
                    if failures:
                        self._append_log("扫描失败项：")
                        for failure in failures:
                            self._append_log("  " + failure)
                    if not ordered:
                        self._append_log("未发现资源分类页面；请检查课程链接或课程页面是否提供按类型浏览。")
                    self.cancel_event.clear()
                elif kind == "done":
                    _, totals, failures, was_cancelled = event
                    self.scan_button.configure(state="normal")
                    self.start_button.configure(state="normal" if self.scanned_courses else "disabled")
                    self.cancel_button.configure(state="disabled")
                    summary = "已下载 {} 个，跳过 {} 个，失败 {} 个。".format(totals["downloaded"], totals["skipped"], totals["failed"])
                    if was_cancelled:
                        summary = "任务已取消。" + summary
                    self.status_text.set(summary)
                    self._append_log(summary)
                    if failures:
                        self._append_log("部分失败项：")
                        for failure in failures:
                            self._append_log("  " + failure)
                    self.cancel_event.clear()
        except queue.Empty:
            pass
        self.root.after(150, self._poll_events)


def main() -> None:
    root = tk.Tk()
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
