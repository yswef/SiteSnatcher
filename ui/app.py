"""
app.py — the desktop interface (CustomTkinter).

The GUI never touches the network. It creates a Crawler, runs it in a daemon
thread, and receives events through a thread-safe queue that is drained by a
Tk `after()` loop. That keeps the window responsive at all times.
"""

from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
import webbrowser
from tkinter import filedialog

import customtkinter as ctk

from core.crawler import DEFAULT_EXCLUDES, DEFAULT_UA, CrawlConfig, Crawler
from ui.theme import COLORS, LOG_COLORS, PAD, RADIUS, human_size, human_time

APP_NAME = "SiteSnatcher"
APP_VERSION = "1.0"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class StatCard(ctk.CTkFrame):
    """A small labelled number used in the sidebar."""

    def __init__(self, master, title: str, color: str = COLORS["text"]):
        super().__init__(master, fg_color=COLORS["card"], corner_radius=RADIUS)
        self.grid_columnconfigure(0, weight=1)
        self.value = ctk.CTkLabel(
            self, text="0", text_color=color,
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        self.value.grid(row=0, column=0, padx=12, pady=(10, 0), sticky="w")
        ctk.CTkLabel(
            self, text=title, text_color=COLORS["muted"], font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, padx=12, pady=(0, 10), sticky="w")

    def set(self, text: str) -> None:
        self.value.configure(text=text)


class SiteSnatcherApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} — Offline Website Downloader")
        self.geometry("1120x740")
        self.minsize(980, 660)
        self.configure(fg_color=COLORS["bg"])

        self.events: queue.Queue = queue.Queue()
        self.crawler: Crawler | None = None
        self.worker: threading.Thread | None = None
        self.is_paused = False
        self.last_output = ""

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._drain_events)

        self._log("Ready. Paste a URL and press Start.", "accent")

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #
    def _build_sidebar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=COLORS["sidebar"], corner_radius=0, width=260)
        bar.grid(row=0, column=0, sticky="nsw")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            bar, text="◈  " + APP_NAME, text_color=COLORS["text"],
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(26, 2), sticky="w")
        ctk.CTkLabel(
            bar, text=f"Offline mirror engine · v{APP_VERSION}",
            text_color=COLORS["muted"], font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, padx=20, pady=(0, 22), sticky="w")

        self.cards: dict[str, StatCard] = {}
        specs = [
            ("pages", "PAGES", COLORS["accent"]),
            ("assets", "ASSETS", COLORS["text"]),
            ("failed", "FAILED", COLORS["error"]),
            ("bytes", "DOWNLOADED", COLORS["ok"]),
            ("queued", "IN QUEUE", COLORS["warn"]),
            ("elapsed", "ELAPSED", COLORS["muted"]),
        ]
        for index, (key, title, color) in enumerate(specs):
            card = StatCard(bar, title, color)
            card.grid(row=2 + index, column=0, padx=18, pady=5, sticky="ew")
            self.cards[key] = card

        bar.grid_rowconfigure(2 + len(specs), weight=1)

        self.open_btn = ctk.CTkButton(
            bar, text="Open output folder", command=self._open_output,
            fg_color=COLORS["card"], hover_color=COLORS["card_hover"],
            text_color=COLORS["text"], corner_radius=RADIUS, height=38, state="disabled",
        )
        self.open_btn.grid(row=3 + len(specs), column=0, padx=18, pady=(6, 6), sticky="ew")

        self.preview_btn = ctk.CTkButton(
            bar, text="Preview offline site", command=self._open_preview,
            fg_color=COLORS["card"], hover_color=COLORS["card_hover"],
            text_color=COLORS["text"], corner_radius=RADIUS, height=38, state="disabled",
        )
        self.preview_btn.grid(row=4 + len(specs), column=0, padx=18, pady=(0, 20), sticky="ew")

    def _build_main(self) -> None:
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=PAD, pady=PAD)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        # ---------- top card: target + destination + actions ----------
        top = ctk.CTkFrame(main, fg_color=COLORS["card"], corner_radius=RADIUS)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top, text="Website URL", text_color=COLORS["muted"],
                     font=ctk.CTkFont(size=12)).grid(
            row=0, column=0, padx=(18, 10), pady=(18, 4), sticky="w")
        self.url_entry = ctk.CTkEntry(
            top, placeholder_text="https://example.com", height=42,
            fg_color=COLORS["field"], border_color=COLORS["border"],
            corner_radius=10, font=ctk.CTkFont(size=14),
        )
        self.url_entry.grid(row=1, column=0, columnspan=2, padx=18, pady=(0, 10), sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._start())

        ctk.CTkLabel(top, text="Save to", text_color=COLORS["muted"],
                     font=ctk.CTkFont(size=12)).grid(
            row=2, column=0, padx=(18, 10), pady=(2, 4), sticky="w")
        self.dir_entry = ctk.CTkEntry(
            top, height=42, fg_color=COLORS["field"], border_color=COLORS["border"],
            corner_radius=10, font=ctk.CTkFont(size=13),
        )
        self.dir_entry.grid(row=3, column=0, padx=(18, 8), pady=(0, 16), sticky="ew")
        self.dir_entry.insert(0, os.path.join(os.path.expanduser("~"), "SiteSnatcher"))
        ctk.CTkButton(
            top, text="Browse", width=110, height=42, command=self._browse,
            fg_color=COLORS["field"], hover_color=COLORS["card_hover"],
            text_color=COLORS["text"], corner_radius=10,
        ).grid(row=3, column=1, padx=(0, 18), pady=(0, 16), sticky="e")

        actions = ctk.CTkFrame(top, fg_color="transparent")
        actions.grid(row=4, column=0, columnspan=2, padx=18, pady=(0, 18), sticky="ew")
        self.start_btn = ctk.CTkButton(
            actions, text="▸  Start download", height=46, width=200, command=self._start,
            fg_color=COLORS["accent"], hover_color=COLORS["accent_dark"],
            corner_radius=10, font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.start_btn.pack(side="left")
        self.pause_btn = ctk.CTkButton(
            actions, text="❚❚  Pause", height=46, width=130, command=self._toggle_pause,
            fg_color=COLORS["field"], hover_color=COLORS["card_hover"],
            text_color=COLORS["text"], corner_radius=10, state="disabled",
        )
        self.pause_btn.pack(side="left", padx=8)
        self.stop_btn = ctk.CTkButton(
            actions, text="■  Stop", height=46, width=120, command=self._stop,
            fg_color=COLORS["field"], hover_color=COLORS["error"],
            text_color=COLORS["text"], corner_radius=10, state="disabled",
        )
        self.stop_btn.pack(side="left")

        # ---------- tabs: log + settings ----------
        tabs = ctk.CTkTabview(
            main, fg_color=COLORS["card"], corner_radius=RADIUS,
            segmented_button_fg_color=COLORS["field"],
            segmented_button_selected_color=COLORS["accent"],
            segmented_button_selected_hover_color=COLORS["accent_dark"],
            segmented_button_unselected_color=COLORS["field"],
            text_color=COLORS["text"],
        )
        tabs.grid(row=1, column=0, sticky="nsew", pady=(PAD, 0))
        tabs.add("Activity")
        tabs.add("Settings")

        activity = tabs.tab("Activity")
        activity.grid_columnconfigure(0, weight=1)
        activity.grid_rowconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(
            activity, fg_color=COLORS["bg"], corner_radius=10,
            text_color=COLORS["text"], font=ctk.CTkFont(family="Consolas", size=12),
            wrap="none",
        )
        self.log_box.grid(row=0, column=0, sticky="nsew", pady=(4, 8))
        self._init_log_tags()
        ctk.CTkButton(
            activity, text="Clear log", width=100, height=30, command=self._clear_log,
            fg_color=COLORS["field"], hover_color=COLORS["card_hover"],
            text_color=COLORS["muted"], corner_radius=8,
        ).grid(row=1, column=0, sticky="e", pady=(0, 6))

        self._build_settings(tabs.tab("Settings"))

        # ---------- bottom: progress ----------
        bottom = ctk.CTkFrame(main, fg_color=COLORS["card"], corner_radius=RADIUS)
        bottom.grid(row=2, column=0, sticky="ew", pady=(PAD, 0))
        bottom.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(
            bottom, height=10, corner_radius=6,
            fg_color=COLORS["field"], progress_color=COLORS["accent"],
        )
        self.progress.grid(row=0, column=0, padx=18, pady=(16, 8), sticky="ew")
        self.progress.set(0)
        self.status = ctk.CTkLabel(
            bottom, text="Idle", text_color=COLORS["muted"], font=ctk.CTkFont(size=12),
        )
        self.status.grid(row=1, column=0, padx=18, pady=(0, 14), sticky="w")

    def _build_settings(self, tab) -> None:
        tab.grid_columnconfigure((0, 1, 2), weight=1)

        def field(row, col, label, default, width=120):
            holder = ctk.CTkFrame(tab, fg_color="transparent")
            holder.grid(row=row, column=col, padx=10, pady=10, sticky="w")
            ctk.CTkLabel(holder, text=label, text_color=COLORS["muted"],
                         font=ctk.CTkFont(size=11)).pack(anchor="w")
            entry = ctk.CTkEntry(
                holder, width=width, height=36, fg_color=COLORS["field"],
                border_color=COLORS["border"], corner_radius=8,
            )
            entry.pack(anchor="w", pady=(4, 0))
            entry.insert(0, str(default))
            return entry

        self.depth_entry = field(0, 0, "Max link depth", 2)
        self.pages_entry = field(0, 1, "Max pages", 200)
        self.workers_entry = field(0, 2, "Parallel workers", 8)
        self.delay_entry = field(1, 0, "Delay per request (s)", 0.0)
        self.timeout_entry = field(1, 1, "Timeout (s)", 20)
        self.maxfile_entry = field(1, 2, "Max file size (MB)", 50)
        self.retries_entry = field(2, 0, "Retries on timeout", 2)

        prefix_holder = ctk.CTkFrame(tab, fg_color="transparent")
        prefix_holder.grid(row=2, column=1, columnspan=2, padx=10, pady=10, sticky="ew")
        prefix_holder.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            prefix_holder,
            text="Restrict pages to path prefix (e.g. /ar/ — assets are not affected)",
            text_color=COLORS["muted"], font=ctk.CTkFont(size=11),
        ).grid(row=0, column=0, sticky="w")
        self.prefix_entry = ctk.CTkEntry(
            prefix_holder, height=36, fg_color=COLORS["field"],
            border_color=COLORS["border"], corner_radius=8,
            placeholder_text="/ar/",
        )
        self.prefix_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        def switch(row, text, default=True):
            var = ctk.BooleanVar(value=default)
            ctk.CTkSwitch(
                tab, text=text, variable=var, text_color=COLORS["text"],
                progress_color=COLORS["accent"], font=ctk.CTkFont(size=12),
            ).grid(row=row, column=0, columnspan=3, padx=12, pady=6, sticky="w")
            return var

        self.sub_var = switch(3, "Include subdomains (blog.site.com, cdn.site.com …)", False)
        self.ext_var = switch(4, "Download assets hosted on other domains (CDN images, fonts)", True)
        self.robots_var = switch(5, "Respect robots.txt (recommended)", True)

        holder = ctk.CTkFrame(tab, fg_color="transparent")
        holder.grid(row=6, column=0, columnspan=3, padx=10, pady=(10, 4), sticky="ew")
        holder.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(holder, text="User-Agent", text_color=COLORS["muted"],
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="w")
        self.ua_entry = ctk.CTkEntry(
            holder, height=36, fg_color=COLORS["field"],
            border_color=COLORS["border"], corner_radius=8,
        )
        self.ua_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.ua_entry.insert(0, DEFAULT_UA)

        ex_holder = ctk.CTkFrame(tab, fg_color="transparent")
        ex_holder.grid(row=7, column=0, columnspan=3, padx=10, pady=(10, 4), sticky="ew")
        ex_holder.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            ex_holder, text="Exclude URLs matching these regexes (one per line)",
            text_color=COLORS["muted"], font=ctk.CTkFont(size=11),
        ).grid(row=0, column=0, sticky="w")
        self.exclude_box = ctk.CTkTextbox(
            ex_holder, height=110, fg_color=COLORS["field"], corner_radius=8,
            text_color=COLORS["text"], font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.exclude_box.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.exclude_box.insert("1.0", DEFAULT_EXCLUDES)

        ctk.CTkLabel(
            tab,
            text="Only mirror sites you own or are allowed to copy. "
                 "High worker counts with zero delay can overload small servers.",
            text_color=COLORS["muted"], font=ctk.CTkFont(size=11), wraplength=700,
            justify="left",
        ).grid(row=8, column=0, columnspan=3, padx=12, pady=(12, 8), sticky="w")

    # ------------------------------------------------------------------ #
    # logging
    # ------------------------------------------------------------------ #
    def _init_log_tags(self) -> None:
        target = getattr(self.log_box, "_textbox", self.log_box)
        for name, color in LOG_COLORS.items():
            try:
                target.tag_config(name, foreground=color)
            except Exception:
                pass

    def _log(self, message: str, level: str = "info") -> None:
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message + "\n", level)
            self.log_box.see("end")
        except Exception:
            pass

    def _clear_log(self) -> None:
        self.log_box.delete("1.0", "end")

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def _browse(self) -> None:
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, path)

    def _read_int(self, entry, fallback: int) -> int:
        try:
            return max(0, int(float(entry.get().strip())))
        except Exception:
            return fallback

    def _read_float(self, entry, fallback: float) -> float:
        try:
            return max(0.0, float(entry.get().strip()))
        except Exception:
            return fallback

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        url = self.url_entry.get().strip()
        if not url:
            self._log("Please enter a URL first.", "error")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, url)

        out_dir = self.dir_entry.get().strip() or os.path.join(
            os.path.expanduser("~"), "SiteSnatcher"
        )
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            self._log(f"Cannot create output folder: {exc}", "error")
            return

        config = CrawlConfig(
            start_url=url,
            out_dir=out_dir,
            max_depth=self._read_int(self.depth_entry, 2),
            max_pages=max(1, self._read_int(self.pages_entry, 200)),
            workers=max(1, min(32, self._read_int(self.workers_entry, 8))),
            delay=self._read_float(self.delay_entry, 0.0),
            timeout=max(5, self._read_int(self.timeout_entry, 20)),
            max_file_mb=max(1, self._read_int(self.maxfile_entry, 50)),
            include_subdomains=bool(self.sub_var.get()),
            external_assets=bool(self.ext_var.get()),
            obey_robots=bool(self.robots_var.get()),
            user_agent=self.ua_entry.get().strip() or DEFAULT_UA,
            path_prefix=self.prefix_entry.get().strip(),
            exclude_patterns=self.exclude_box.get("1.0", "end").strip(),
            retries=max(0, min(5, self._read_int(self.retries_entry, 2))),
        )

        self.last_output = out_dir
        self.crawler = Crawler(config, lambda kind, payload: self.events.put((kind, payload)))
        self.worker = threading.Thread(target=self.crawler.run, daemon=True)

        self._clear_log()
        for card in self.cards.values():
            card.set("0")
        self.progress.set(0)
        self.progress.configure(progress_color=COLORS["accent"])
        self.status.configure(text="Crawling…", text_color=COLORS["accent"])
        self.start_btn.configure(state="disabled", text="Working…")
        self.pause_btn.configure(state="normal", text="❚❚  Pause")
        self.stop_btn.configure(state="normal")
        self.open_btn.configure(state="normal")
        self.preview_btn.configure(state="disabled")
        self.is_paused = False

        self.worker.start()

    def _toggle_pause(self) -> None:
        if not self.crawler:
            return
        if self.is_paused:
            self.crawler.resume()
            self.pause_btn.configure(text="❚❚  Pause")
            self.status.configure(text="Crawling…", text_color=COLORS["accent"])
        else:
            self.crawler.pause()
            self.pause_btn.configure(text="▸  Resume")
            self.status.configure(text="Paused", text_color=COLORS["warn"])
        self.is_paused = not self.is_paused

    def _stop(self) -> None:
        if self.crawler:
            self.crawler.stop()
            self.status.configure(text="Stopping…", text_color=COLORS["warn"])
            self.stop_btn.configure(state="disabled")

    def _open_output(self) -> None:
        path = self.last_output or self.dir_entry.get().strip()
        if not path or not os.path.isdir(path):
            return
        try:
            if platform.system() == "Windows":
                os.startfile(path)  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            self._log(f"Cannot open folder: {exc}", "error")

    def _open_preview(self) -> None:
        index = os.path.join(self.last_output, "index.html")
        if os.path.isfile(index):
            webbrowser.open("file://" + os.path.abspath(index))

    # ------------------------------------------------------------------ #
    # event pump
    # ------------------------------------------------------------------ #
    def _drain_events(self) -> None:
        processed = 0
        try:
            while processed < 200:
                kind, payload = self.events.get_nowait()
                processed += 1
                if kind == "log":
                    self._log(payload["message"], payload.get("level", "info"))
                elif kind == "stats":
                    self._update_stats(payload)
                elif kind == "rewriting":
                    self.status.configure(
                        text=f"Rewriting links… {payload['done']}/{payload['total']}",
                        text_color=COLORS["accent"],
                    )
                elif kind == "error":
                    self._log("ERROR: " + payload["message"], "error")
                    self._finish(error=True)
                elif kind == "done":
                    self._on_done(payload)
        except queue.Empty:
            pass
        self.after(120, self._drain_events)

    def _update_stats(self, data: dict) -> None:
        self.cards["pages"].set(str(data["pages"]))
        self.cards["assets"].set(str(data["assets"]))
        self.cards["failed"].set(str(data["failed"]))
        self.cards["bytes"].set(human_size(data["bytes"]))
        self.cards["queued"].set(str(data["queued"]))
        self.cards["elapsed"].set(human_time(data["elapsed"]))
        total = max(1, data.get("max_pages", 1))
        self.progress.set(min(1.0, data["pages"] / total))

    def _on_done(self, data: dict) -> None:
        self.progress.set(1.0)
        self.progress.configure(
            progress_color=COLORS["warn"] if data["stopped"] else COLORS["ok"]
        )
        word = "Stopped" if data["stopped"] else "Completed"
        self._log(
            f"{word}: {data['pages']} pages, {data['assets']} assets, "
            f"{data['failed']} failed, {human_size(data['bytes'])} on disk.",
            "accent",
        )
        self._log(f"Saved in: {data['out_dir']}", "accent")
        self.status.configure(
            text=f"{word} — open the folder or preview the mirror",
            text_color=COLORS["ok"] if not data["stopped"] else COLORS["warn"],
        )
        self.preview_btn.configure(state="normal")
        self._finish()

    def _finish(self, error: bool = False) -> None:
        self.start_btn.configure(state="normal", text="▸  Start download")
        self.pause_btn.configure(state="disabled", text="❚❚  Pause")
        self.stop_btn.configure(state="disabled")
        if error:
            self.status.configure(text="Failed", text_color=COLORS["error"])

    def _on_close(self) -> None:
        if self.crawler:
            self.crawler.stop()
        self.destroy()


def run() -> None:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("Tkinter is missing. On Debian/Ubuntu: sudo apt install python3-tk")
        sys.exit(1)
    SiteSnatcherApp().mainloop()