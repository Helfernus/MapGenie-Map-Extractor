from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from map_extractor import APP_VERSION, MapGenieClient, MapGenieError, describe_map_with_availability, format_elapsed, open_folder, process_map

DEFAULT_URL = "https://mapgenie.io/grand-theft-auto-3/maps/liberty-city"


class ExtractorGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"MapGenie / Branded Highest-Detail Map Extractor v{APP_VERSION}")
        self.geometry("940x720")
        self.minsize(800, 600)
        self.events: queue.Queue[tuple] = queue.Queue()

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(11, weight=1)

        self.url_var = self._var(tk.StringVar, DEFAULT_URL)
        self.out_var = self._var(tk.StringVar, str((Path.cwd() / "output").resolve()))
        self.all_maps_var = self._var(tk.BooleanVar, False)
        self.stitch_var = self._var(tk.BooleanVar, True)
        self.open_output_var = self._var(tk.BooleanVar, True)
        self.concurrency_var = self._var(tk.IntVar, 4)
        self.request_delay_var = self._var(tk.DoubleVar, 0.12)
        self.retries_var = self._var(tk.IntVar, 5)
        self.transport_var = self._var(tk.StringVar, "auto")
        self.zoom_var = self._var(tk.StringVar, "auto")
        self.ca_var = self._var(tk.StringVar, "")
        self.insecure_var = self._var(tk.BooleanVar, False)
        self.tls_var = self._var(tk.StringVar, MapGenieClient.tls_status())
        self.status_var = self._var(tk.StringVar, "Ready")

        ttk.Label(frame, text="Map page URL").grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(frame, textvariable=self.url_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="Output folder").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(frame, text="Browse…", command=self.choose_output).grid(row=1, column=2, padx=(8, 0), pady=6)

        ttk.Checkbutton(
            frame,
            text="Also discover sibling map pages (canonical mapgenie.io URLs only; tile sets are always all processed)",
            variable=self.all_maps_var,
        ).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Checkbutton(
            frame,
            text="Stitch each tile set's highest downloadable zoom into one PNG",
            variable=self.stitch_var,
        ).grid(row=3, column=1, sticky="w", pady=3)

        ttk.Label(frame, text="Download pacing").grid(row=4, column=0, sticky="w", pady=6)
        pacing = ttk.Frame(frame)
        pacing.grid(row=4, column=1, columnspan=2, sticky="w", pady=6)
        for label, variable, start, end, step, width in (
            ("Workers", self.concurrency_var, 1, 24, 1, 5),
            ("Delay (s)", self.request_delay_var, 0.0, 5.0, 0.05, 7),
            ("Retries", self.retries_var, 1, 12, 1, 5),
        ):
            ttk.Label(pacing, text=label).pack(side="left")
            ttk.Spinbox(pacing, from_=start, to=end, increment=step, textvariable=variable, width=width).pack(
                side="left", padx=(5, 14)
            )
        ttk.Label(pacing, text="Transport").pack(side="left")
        ttk.Combobox(
            pacing, textvariable=self.transport_var, values=("auto", "chrome", "requests"), state="readonly",
            width=10
        ).pack(side="left", padx=(5, 14))
        ttk.Label(pacing, text="Max zoom").pack(side="left")
        ttk.Entry(pacing, textvariable=self.zoom_var, width=6).pack(side="left", padx=(5, 0))

        ttk.Label(frame, text="Custom CA bundle").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.ca_var).grid(row=5, column=1, sticky="ew", pady=6)
        ttk.Button(frame, text="Browse…", command=self.choose_ca).grid(row=5, column=2, padx=(8, 0), pady=6)
        ttk.Checkbutton(
            frame,
            text="Disable HTTPS certificate verification (unsafe; last resort only)",
            variable=self.insecure_var,
        ).grid(row=6, column=1, sticky="w", pady=3)
        ttk.Label(frame, textvariable=self.tls_var).grid(row=7, column=0, columnspan=3, sticky="w", pady=(3, 6))

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=1, sticky="w", pady=(8, 10))
        self.analyze_button = ttk.Button(buttons, text="Analyze", command=self.analyze)
        self.run_button = ttk.Button(buttons, text="Download + Stitch", command=self.run_extraction)
        self.analyze_button.pack(side="left")
        self.run_button.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(buttons, text="Open folder when done", variable=self.open_output_var).pack(side="left", padx=12)

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Label(frame, textvariable=self.status_var).grid(row=10, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.log = tk.Text(frame, wrap="word")
        self.log.grid(row=11, column=0, columnspan=3, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        scroll.grid(row=11, column=3, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self.after(100, self.poll_events)

    def _var(self, kind, value):
        return kind(master=self, value=value)

    def choose_output(self):
        if path := filedialog.askdirectory(initialdir=self.out_var.get()):
            self.out_var.set(path)

    def choose_ca(self):
        if path := filedialog.askopenfilename(
            title="Select PEM CA bundle",
            filetypes=[("PEM certificates", "*.pem *.crt *.cer"), ("All files", "*.*")],
        ):
            self.ca_var.set(path)

    def _make_client(self) -> MapGenieClient:
        if self.insecure_var.get():
            return MapGenieClient(verify=False)
        if ca := self.ca_var.get().strip():
            path = Path(ca).expanduser().resolve()
            if not path.is_file():
                raise MapGenieError(f"CA bundle not found: {path}")
            return MapGenieClient(verify=str(path))
        return MapGenieClient()

    def _common(self):
        zoom = self.zoom_var.get().strip().lower()
        return (
            self._make_client(), self.url_var.get().strip(), self.all_maps_var.get(),
            self.transport_var.get().strip() or "auto", None if zoom in ("", "auto") else int(zoom),
        )

    def _urls(self, client: MapGenieClient, url: str, all_maps: bool) -> list[str]:
        return client.map_urls_for_game(url) if all_maps else [url]

    def _tls_log(self, client: MapGenieClient):
        verify = "DISABLED (unsafe)" if client.verify is False else client.verify
        self.events.put(("log", f"{client.tls_status()} | effective verify={verify}"))

    def append(self, text: str):
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.analyze_button.configure(state=state)
        self.run_button.configure(state=state)

    def _fail(self, exc: Exception):
        self.set_busy(False)
        self.status_var.set("Error")
        self.append("ERROR: " + str(exc))
        messagebox.showerror("Map extractor", str(exc))

    def _launch(self, status: str, factory):
        self.set_busy(True)
        self.log.delete("1.0", "end")
        self.status_var.set(status)
        try:
            worker = factory()
        except Exception as exc:
            self._fail(exc)
            return

        def wrapped():
            try:
                worker()
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=wrapped, daemon=True).start()

    def analyze(self):
        def factory():
            client, url, all_maps, transport, zoom = self._common()

            def work():
                self._tls_log(client)
                urls = self._urls(client, url, all_maps)
                for i, item in enumerate(urls, 1):
                    info = client.inspect_map(item)
                    self.events.put(("log", describe_map_with_availability(info, client.verify, transport, zoom)
                                     + "\n"))
                    self.events.put(("progress", f"Analyzed {i}/{len(urls)}", i, len(urls)))
                self.events.put(("done", "Analysis complete"))

            return work

        self._launch("Analyzing…", factory)

    def run_extraction(self):
        def factory():
            client, url, all_maps, transport, zoom = self._common()
            output = Path(self.out_var.get()).expanduser().resolve()
            concurrency = max(1, int(self.concurrency_var.get()))
            delay = max(0.0, float(self.request_delay_var.get()))
            retries = max(1, int(self.retries_var.get()))
            stitch = self.stitch_var.get()
            open_output = self.open_output_var.get()

            def progress(message: str, current: int, total: int):
                self.events.put(("progress", message, current, total))

            def work():
                started = time.perf_counter()
                self._tls_log(client)
                urls = self._urls(client, url, all_maps)
                outputs = []
                for i, item in enumerate(urls, 1):
                    self.events.put(("log", f"Processing {item}"))
                    outputs += process_map(
                        client,
                        item,
                        output,
                        concurrency,
                        stitch,
                        None,
                        progress,
                        retries,
                        delay,
                        transport,
                        zoom,
                    )
                    self.events.put(("log", f"Finished map {i}/{len(urls)}\n"))
                if outputs:
                    self.events.put(("log", "Stitched PNG files:"))
                    for path in outputs:
                        self.events.put(("log", f"  {path}"))
                elapsed = format_elapsed(time.perf_counter() - started)
                self.events.put(("done", f"Extraction complete — {elapsed}", output if open_output else None))

            return work

        self._launch("Starting…", factory)

    def poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.append(event[1])
                elif kind == "progress":
                    _, message, current, total = event
                    pct = current * 100 / total if total else 0
                    self.progress["value"] = pct
                    self.status_var.set(f"{message} — {current}/{total} ({pct:.1f}%)" if total else message)
                elif kind == "done":
                    self.progress["value"] = 100
                    self.status_var.set(event[1])
                    self.set_busy(False)
                    if len(event) > 2 and event[2]:
                        try:
                            open_folder(event[2])
                        except OSError as exc:
                            self.append(f"Could not open output folder: {exc}")
                    messagebox.showinfo("Map extractor", event[1])
                elif kind == "error":
                    self._fail(RuntimeError(event[1]))
        except queue.Empty:
            pass
        self.after(100, self.poll_events)


if __name__ == "__main__":
    try:
        ExtractorGUI().mainloop()
    except MapGenieError as exc:
        messagebox.showerror("Map extractor", str(exc))
