"""
CourtListener GUI
=================
A Tkinter interface for searching US case law via the CourtListener API
and downloading opinion PDFs.

Requires:
    pip install requests

Usage:
    python courtlistener_gui.py

Token lookup order:
  1. COURTLISTENER_TOKEN environment variable
  2. ~/.config/courtlistener/config.json  (saved automatically after first use)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from courtlistener import COURTS, CourtListenerClient, CourtListenerError

_CONFIG_PATH = Path.home() / ".config" / "courtlistener" / "config.json"


def _load_saved_token() -> str:
    """Return the token saved in the config file, or '' if none."""
    try:
        data = json.loads(_CONFIG_PATH.read_text())
        return data.get("api_token", "")
    except Exception:
        return ""


def _save_token(token: str) -> None:
    """Persist *token* to the config file."""
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(json.dumps({"api_token": token}))
    except Exception:
        pass  # Non-fatal – token simply won't persist


# Routing logic mirrors https://github.com/birds-inc/scotuslink:
#   vols 1-542  → LOC CDN per-opinion PDFs (volume and page both 3-digit zero-padded)
#   vols 543+   → supremecourt.gov bound-volume PDFs with #page= anchor
# The #page= anchor for SCOTUS bound volumes uses the US Reports page number as a
# best-effort approximation (PDF page numbers differ slightly due to front matter).
_LOC_CUTOFF = 542
_US_CITE_RE = re.compile(r"(\d+)\s+U\.S\.\s+(\d+)")


def _us_reports_pdf_url(citation: str) -> Optional[str]:
    """
    Return the official US Reports PDF URL for a citation like '410 U.S. 113'.

        vols 1-542  → cdn.loc.gov per-opinion PDF (exact document)
        vols 543+   → supremecourt.gov bound-volume PDF with #page anchor
    """
    m = _US_CITE_RE.search(citation)
    if not m:
        return None
    vol, page = int(m.group(1)), int(m.group(2))
    if vol <= _LOC_CUTOFF:
        return (
            f"https://cdn.loc.gov/service/ll/usrep/"
            f"usrep{vol:03d}/usrep{vol:03d}{page:03d}/usrep{vol:03d}{page:03d}.pdf"
        )
    return f"https://www.supremecourt.gov/opinions/boundvolumes/{vol}BV.pdf#page={page:03d}"


try:
    from google_scholar import GoogleScholarFetcher

    _SCHOLAR_AVAILABLE = True
except ImportError:
    _SCHOLAR_AVAILABLE = False


class CourtListenerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CourtListener Case Law Search")
        self.root.geometry("1050x680")
        self.root.minsize(800, 500)

        self._client: Optional[CourtListenerClient] = None
        self._results: list[dict] = []
        self._search_thread: Optional[threading.Thread] = None
        self._scholar: Optional["GoogleScholarFetcher"] = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.configure("Treeview", rowheight=28)

        # --- Token row ---
        token_frame = ttk.LabelFrame(self.root, text="API Token", padding=6)
        token_frame.pack(fill="x", padx=10, pady=(10, 4))

        initial_token = os.environ.get("COURTLISTENER_TOKEN") or _load_saved_token()
        self._token_var = tk.StringVar(value=initial_token)
        ttk.Label(token_frame, text="Token:").pack(side="left")
        self._token_entry = ttk.Entry(
            token_frame, textvariable=self._token_var, show="*", width=55
        )
        self._token_entry.pack(side="left", padx=6)
        self._show_token_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            token_frame,
            text="Show",
            variable=self._show_token_var,
            command=self._toggle_token_vis,
        ).pack(side="left")

        # --- Search frame ---
        search_frame = ttk.LabelFrame(self.root, text="Search", padding=6)
        search_frame.pack(fill="x", padx=10, pady=4)

        # Row 1: query + button
        row1 = ttk.Frame(search_frame)
        row1.pack(fill="x", pady=(0, 4))
        ttk.Label(row1, text="Query:").pack(side="left")
        self._query_var = tk.StringVar()
        self._query_entry = ttk.Entry(row1, textvariable=self._query_var)
        self._query_entry.pack(side="left", padx=6, fill="x", expand=True)
        self._query_entry.bind("<Return>", lambda _e: self._do_search())
        self._search_btn = ttk.Button(row1, text="Search", command=self._do_search)
        self._search_btn.pack(side="left", padx=(0, 4))

        # Row 2: filters
        row2 = ttk.Frame(search_frame)
        row2.pack(fill="x")

        ttk.Label(row2, text="Court:").pack(side="left")
        self._court_var = tk.StringVar(value="(any)")
        court_choices = ["(any)"] + sorted(COURTS.keys())
        ttk.Combobox(
            row2,
            textvariable=self._court_var,
            values=court_choices,
            width=10,
            state="readonly",
        ).pack(side="left", padx=(4, 12))

        ttk.Label(row2, text="Filed from:").pack(side="left")
        self._date_from_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self._date_from_var, width=12).pack(
            side="left", padx=4
        )

        ttk.Label(row2, text="to:").pack(side="left")
        self._date_to_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self._date_to_var, width=12).pack(
            side="left", padx=4
        )

        ttk.Label(row2, text="  Max results:").pack(side="left")
        self._page_size_var = tk.IntVar(value=20)
        ttk.Spinbox(
            row2, from_=5, to=20, textvariable=self._page_size_var, width=5
        ).pack(side="left", padx=4)

        # --- Results table ---
        results_frame = ttk.LabelFrame(self.root, text="Results", padding=6)
        results_frame.pack(fill="both", expand=True, padx=10, pady=4)

        cols = ("case_name", "court", "date_filed", "citation", "status")
        self._tree = ttk.Treeview(
            results_frame, columns=cols, show="headings", selectmode="browse"
        )
        self._tree.heading("case_name", text="Case Name")
        self._tree.heading("court", text="Court")
        self._tree.heading("date_filed", text="Date Filed")
        self._tree.heading("citation", text="Citation")
        self._tree.heading("status", text="Status")

        self._tree.column("case_name", width=390, minwidth=200)
        self._tree.column("court", width=75, minwidth=60, anchor="center")
        self._tree.column("date_filed", width=90, minwidth=80, anchor="center")
        self._tree.column("citation", width=160, minwidth=100)
        self._tree.column("status", width=120, minwidth=80)

        vsb = ttk.Scrollbar(results_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._tree.bind("<Double-1>", lambda _e: self._download_selected())
        self._tree.bind("<<TreeviewSelect>>", self._on_row_select)

        # --- Status bar + download button ---
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=10, pady=(2, 10))

        self._download_btn = ttk.Button(
            bottom,
            text="Download PDF",
            command=self._download_selected,
            state="disabled",
        )
        self._download_btn.pack(side="right", padx=4)

        scholar_tip = "" if _SCHOLAR_AVAILABLE else " (needs beautifulsoup4)"
        self._scholar_btn = ttk.Button(
            bottom,
            text=f"Scholar Text{scholar_tip}",
            command=self._fetch_scholar_text,
            state="disabled",
        )
        self._scholar_btn.pack(side="right", padx=4)

        self._status_var = tk.StringVar(value="Enter a query and click Search.")
        ttk.Label(bottom, textvariable=self._status_var, anchor="w").pack(
            side="left", fill="x", expand=True
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _toggle_token_vis(self) -> None:
        self._token_entry.config(show="" if self._show_token_var.get() else "*")

    def _on_row_select(self, _event=None) -> None:
        if self._tree.selection():
            self._download_btn.config(state="normal")
            self._scholar_btn.config(state="normal")

    def _get_client(self) -> Optional[CourtListenerClient]:
        token = self._token_var.get().strip()
        if not token:
            messagebox.showerror(
                "Missing Token", "Please enter your CourtListener API token."
            )
            return None
        if self._client is None or self._client._session.headers.get(
            "Authorization"
        ) != f"Token {token}":
            self._client = CourtListenerClient(api_token=token)
            _save_token(token)
        return self._client

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _do_search(self) -> None:
        if self._search_thread and self._search_thread.is_alive():
            return

        client = self._get_client()
        if client is None:
            return

        query = self._query_var.get().strip()
        if not query:
            messagebox.showwarning("Empty Query", "Please enter a search query.")
            return

        court = self._court_var.get()
        if court == "(any)":
            court = None
        date_from = self._date_from_var.get().strip() or None
        date_to = self._date_to_var.get().strip() or None
        page_size = self._page_size_var.get()

        # Clear previous results
        self._search_btn.config(state="disabled")
        self._download_btn.config(state="disabled")
        self._scholar_btn.config(state="disabled")
        self._status_var.set("Searching…")
        for row in self._tree.get_children():
            self._tree.delete(row)
        self._results.clear()

        def run() -> None:
            try:
                data = client.search(
                    query,
                    type="o",
                    court=court,
                    date_filed_min=date_from,
                    date_filed_max=date_to,
                    page_size=page_size,
                )
                self.root.after(0, self._on_results, data)
            except CourtListenerError as exc:
                self.root.after(0, self._on_error, str(exc))
            except Exception as exc:
                self.root.after(0, self._on_error, f"Unexpected error: {exc}")

        self._search_thread = threading.Thread(target=run, daemon=True)
        self._search_thread.start()

    def _on_results(self, data: dict) -> None:
        self._search_btn.config(state="normal")
        results = data.get("results", [])
        count = data.get("count", len(results))
        self._results = results

        for i, item in enumerate(results):
            case_name = item.get("caseName") or item.get("case_name") or "(unknown)"
            court = item.get("court") or item.get("court_id") or ""
            date_filed = item.get("dateFiled") or item.get("date_filed") or ""
            citations = item.get("citation", [])
            if isinstance(citations, list):
                us_reports = next((c for c in citations if " U.S. " in c), None)
                citation_str = us_reports or (citations[0] if citations else "")
            else:
                citation_str = str(citations) if citations else ""
            status = (
                item.get("precedentialStatus") or item.get("precedential_status") or ""
            )
            self._tree.insert(
                "",
                "end",
                iid=str(i),
                values=(case_name, court, date_filed, citation_str, status),
            )

        if results:
            self._status_var.set(
                f"Showing {len(results)} of {count:,} results. "
                "Select a row and click Download PDF (or double-click)."
            )
        else:
            self._status_var.set("No results found.")

    def _on_error(self, message: str) -> None:
        self._search_btn.config(state="normal")
        self._status_var.set(f"Error: {message}")
        messagebox.showerror("API Error", message)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _download_selected(self) -> None:
        selection = self._tree.selection()
        if not selection:
            messagebox.showinfo("No Selection", "Please select a case first.")
            return

        idx = int(selection[0])
        item = self._results[idx]

        case_name = item.get("caseName") or item.get("case_name") or "opinion"
        safe_name = "".join(
            c if c.isalnum() or c in " -_" else "_" for c in case_name
        )[:80].strip()

        save_path = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            initialfile=f"{safe_name}.pdf",
            title="Save Opinion PDF",
        )
        if not save_path:
            return

        client = self._get_client()
        if client is None:
            return

        self._status_var.set("Resolving PDF URL…")
        self._download_btn.config(state="disabled")
        self._scholar_btn.config(state="disabled")
        self._search_btn.config(state="disabled")

        def run() -> None:
            try:
                print(f"\n[download] raw item keys: {list(item.keys())}")
                print(f"[download] local_path   = {item.get('local_path') or item.get('localPath')!r}")
                print(f"[download] download_url = {item.get('download_url')!r}")
                print(f"[download] cluster_id   = {item.get('cluster_id') or item.get('id')!r}")

                pdf_url = self._resolve_pdf_url(client, item)
                print(f"[download] resolved url = {pdf_url!r}")

                if not pdf_url:
                    self.root.after(
                        0,
                        self._on_error,
                        "No downloadable PDF found for this opinion.\n\n"
                        "The source document may only be available as HTML.",
                    )
                    return

                self.root.after(0, self._status_var.set, f"Downloading… {pdf_url}")
                print(f"[download] fetching {pdf_url}")
                response = client._session.get(pdf_url, timeout=60, stream=True)
                print(f"[download] HTTP {response.status_code}  content-type: {response.headers.get('content-type')}")
                response.raise_for_status()

                with open(save_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)

                self.root.after(0, self._on_download_done, save_path)
            except Exception as exc:
                self.root.after(0, self._on_error, f"Download failed: {exc}")
            finally:
                self.root.after(0, self._restore_buttons)

        threading.Thread(target=run, daemon=True).start()

    def _resolve_pdf_url(
        self, client: CourtListenerClient, item: dict
    ) -> Optional[str]:
        """
        Attempt to find a PDF URL for the selected search result.

        Strategy:
        0. If a US Reports citation is present, use the official LOC/GovInfo PDF.
        1. Use local_path from the search result (stored on CourtListener's servers).
        2. Use download_url from the search result (original source — may not be .pdf).
        3. Fetch the cluster's sub_opinions and check each opinion for
           local_path or download_url.
        """
        storage_base = "https://storage.courtlistener.com/"

        # 0. Official US Reports PDF (LOC or GovInfo) — highest fidelity source
        citations = item.get("citation", [])
        if isinstance(citations, list):
            us_cite = next((c for c in citations if " U.S. " in c), None)
        else:
            us_cite = str(citations) if citations and " U.S. " in str(citations) else None
        if us_cite:
            official_url = _us_reports_pdf_url(us_cite)
            if official_url:
                print(f"[resolve] using official US Reports PDF: {official_url}")
                return official_url

        # 1. local_path on the search result (most reliable — CourtListener's own copy)
        local = item.get("local_path") or item.get("localPath") or ""
        if local:
            return storage_base + local.lstrip("/")

        # 2. download_url on the search result (original court source)
        #    Note: court URLs often don't end in ".pdf" even when they are PDFs.
        url = item.get("download_url") or ""
        if url:
            return url

        # 3. Fetch the opinion directly by its ID (search results return opinion-level
        #    rows where 'id' is the opinion ID and 'cluster_id' is the cluster).
        opinion_id = item.get("id")
        if opinion_id:
            try:
                print(f"[resolve] fetching opinion {opinion_id} directly")
                op = client.get_opinion(int(opinion_id))
                print(f"[resolve] opinion keys: {list(op.keys())}")
                print(f"[resolve] opinion local_path = {op.get('local_path')!r}")
                print(f"[resolve] opinion download_url = {op.get('download_url')!r}")
                local = op.get("local_path") or ""
                if local:
                    return storage_base + local.lstrip("/")
                dl = op.get("download_url") or ""
                if dl:
                    return dl
            except Exception as exc:
                print(f"[resolve] direct opinion fetch failed: {exc}")

        # 4. Fall back to cluster → sub_opinions walk
        cluster_id = item.get("cluster_id") or item.get("id")
        if cluster_id:
            try:
                print(f"[resolve] fetching cluster {cluster_id}")
                cluster = client.get_cluster(int(cluster_id), fields="sub_opinions")
                print(f"[resolve] sub_opinions = {cluster.get('sub_opinions')!r}")
                for op_url in cluster.get("sub_opinions", []):
                    print(f"[resolve] fetching sub-opinion {op_url}")
                    op = client._get_url(op_url, {"fields": "download_url,local_path"})
                    print(f"[resolve]   local_path={op.get('local_path')!r}  download_url={op.get('download_url')!r}")
                    local = op.get("local_path") or ""
                    if local:
                        return storage_base + local.lstrip("/")
                    dl = op.get("download_url") or ""
                    if dl:
                        return dl
            except Exception as exc:
                print(f"[resolve] cluster walk failed: {exc}")

        return None

    # ------------------------------------------------------------------
    # Google Scholar text fetch
    # ------------------------------------------------------------------

    def _get_scholar(self) -> Optional["GoogleScholarFetcher"]:
        if not _SCHOLAR_AVAILABLE:
            messagebox.showerror(
                "Missing Dependency",
                "Google Scholar fetching requires beautifulsoup4.\n\n"
                "Install it with:\n    pip install beautifulsoup4",
            )
            return None
        if self._scholar is None:
            self._scholar = GoogleScholarFetcher()
        return self._scholar

    def _fetch_scholar_text(self) -> None:
        selection = self._tree.selection()
        if not selection:
            messagebox.showinfo("No Selection", "Please select a case first.")
            return

        fetcher = self._get_scholar()
        if fetcher is None:
            return

        idx = int(selection[0])
        item = self._results[idx]

        citations = item.get("citation", [])
        citation_str = citations[0] if isinstance(citations, list) and citations else ""
        case_name = item.get("caseName") or item.get("case_name") or ""
        date_filed = item.get("dateFiled") or item.get("date_filed") or ""
        year = date_filed[:4] if date_filed else None

        self._download_btn.config(state="disabled")
        self._scholar_btn.config(state="disabled")
        self._search_btn.config(state="disabled")
        self._status_var.set("Searching Google Scholar…")

        def run() -> None:
            result = None
            if citation_str:
                print(f"[scholar] trying citation: {citation_str!r}")
                result = fetcher.fetch_by_citation(citation_str)
            if result is None and case_name:
                print(f"[scholar] falling back to case name: {case_name!r} ({year})")
                result = fetcher.fetch_by_name(case_name, year)

            self.root.after(0, self._on_scholar_result, result)

        threading.Thread(target=run, daemon=True).start()

    def _on_scholar_result(self, result: Optional[tuple[str, str]]) -> None:
        self._restore_buttons()
        if result is None:
            self._status_var.set("Google Scholar: no text found.")
            messagebox.showwarning(
                "Scholar Not Found",
                "Could not find this opinion on Google Scholar.\n\n"
                "Google may have blocked the request, or the case may not be indexed.\n"
                "Check the terminal for details.",
            )
            return

        url, text = result
        self._status_var.set(f"Scholar text loaded from {url}")
        self._show_scholar_window(url, text)

    def _show_scholar_window(self, url: str, text: str) -> None:
        win = tk.Toplevel(self.root)
        win.title("Google Scholar Opinion Text")
        win.geometry("800x600")

        # URL bar
        url_frame = ttk.Frame(win)
        url_frame.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(url_frame, text="Source:").pack(side="left")
        url_var = tk.StringVar(value=url)
        ttk.Entry(url_frame, textvariable=url_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=4
        )

        # Text area
        text_frame = ttk.Frame(win)
        text_frame.pack(fill="both", expand=True, padx=8, pady=4)
        txt = tk.Text(text_frame, wrap="word", font=("TkDefaultFont", 10))
        vsb = ttk.Scrollbar(text_frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", text)
        txt.config(state="disabled")

        # Save button
        def save_text() -> None:
            path = filedialog.asksaveasfilename(
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                title="Save Opinion Text",
                parent=win,
            )
            if path:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                messagebox.showinfo("Saved", f"Text saved to:\n{path}", parent=win)

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_frame, text="Save as .txt…", command=save_text).pack(side="right")
        ttk.Label(
            btn_frame,
            text=f"{len(text):,} characters  |  cached locally",
            foreground="gray",
        ).pack(side="left")

    def _restore_buttons(self) -> None:
        self._download_btn.config(state="normal")
        self._scholar_btn.config(state="normal")
        self._search_btn.config(state="normal")

    def _on_download_done(self, path: str) -> None:
        self._status_var.set(f"Saved: {path}")
        if messagebox.askyesno(
            "Download Complete", f"PDF saved to:\n{path}\n\nOpen it now?"
        ):
            self._open_file(path)

    @staticmethod
    def _open_file(path: str) -> None:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])


def main() -> None:
    root = tk.Tk()
    CourtListenerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
