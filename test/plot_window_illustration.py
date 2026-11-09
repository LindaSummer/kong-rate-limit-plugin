"""Plot saved Parquet request results, offline; never start Docker or send traffic.

uv run --no-project --with matplotlib==3.10.8 --with pyarrow==22.0.0 python test/plot_window_illustration.py /tmp/capture/requests.parquet --bin-ms 100
"""
import argparse
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def load(path):
    table = pq.read_table(path)
    metadata = json.loads(table.schema.metadata[b"kong.window_illustration"])
    columns = {name: table[name].cast(pa.int64()).to_pylist() if pa.types.is_timestamp(table[name].type)
               else table[name].to_pylist() for name in table.column_names}
    return metadata, [dict(zip(columns, values)) for values in zip(*columns.values())]


def plot(path, output, bin_ms, accepted_only=False, boundary_seconds=None):
    data, rows = load(path)
    output.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), sharey="row", layout="constrained")
    trends, trend_axes = plt.subplots(2, 2, figsize=(13, 7), sharey="row", layout="constrained")
    summary = []
    bin_seconds = bin_ms / 1000
    bucket_label = f"{bin_seconds:g} s" if bin_ms >= 1000 and bin_ms % 1000 == 0 else f"{bin_ms:g} ms"
    for row, case in enumerate(("boundary", "fresh")):
        reference = data["boundary_ms"] if case == "boundary" else data["fresh_burst_ms"]
        reference_ns = reference * 1_000_000
        case_rows = [r for r in rows if r["scenario"] == case
                     and (case != "boundary" or boundary_seconds is None
                          or r["timestamp"] < (reference + boundary_seconds * 1000) * 1_000_000)]
        offsets = [(r[key] - reference_ns) / 1e9 for r in case_rows
                   for key in ("timestamp", "upstream_timestamp") if r[key] is not None]
        left = math.floor(min([-2 if case == "boundary" else -1] + offsets) / bin_seconds) * bin_seconds
        minimum_right = (boundary_seconds or data.get("duration_seconds", 3)) if case == "boundary" else 3
        right = math.ceil(max([minimum_right] + offsets) / bin_seconds) * bin_seconds
        bins = left + np.arange(round((right - left) / bin_seconds) + 1) * bin_seconds
        for col, algorithm in enumerate(("fixed_window", "sliding_window")):
            ax = axes[row, col]
            trend = trend_axes[row, col]
            items = [r for r in case_rows if r["algorithm"] == algorithm]
            offered = [(r["timestamp"] - reference_ns) / 1e9 for r in items]
            arrived = [(r["upstream_timestamp"] - reference_ns) / 1e9 for r in items if r["upstream_timestamp"] is not None]
            rejected = [(r["timestamp"] - reference_ns) / 1e9 for r in items if r["return_code"] == 429]
            offered_counts, _ = np.histogram(offered, bins)
            upstream_counts, _ = np.histogram(arrived, bins)
            rejected_counts, _ = np.histogram(rejected, bins)
            assert int(offered_counts.sum()) == len(items) and int(upstream_counts.sum()) == len(arrived)
            if not accepted_only:
                ax.bar(bins[:-1], offered_counts, width=bin_seconds, align="edge", facecolor="none",
                       edgecolor="#9ca3af", linewidth=1.2, label="Total attempted")
            ax.bar(bins[:-1], upstream_counts, width=bin_seconds * 0.8, align="edge",
                   color="#2563eb", label="Accepted requests (observed at upstream)")
            if not accepted_only:
                ax.stairs(rejected_counts, bins, color="#dc2626", linestyle="--", linewidth=1.3,
                          label="Returned 429 (by request time)")
            if case == "boundary":
                for boundary in np.arange(0, right + 0.001, data["window_ms"] / 1000):
                    ax.axvline(boundary, color="#374151", linestyle=":", linewidth=1, alpha=0.5)
                ax.axvspan(left, 0, color="#e5e7eb", alpha=0.3)
            ax.set_xlim(left, right)
            label = "Fixed" if col == 0 else "Sliding"
            ax.set_title(label + ((" — boundary bursts" if boundary_seconds else " — boundary bursts and recovery") if row == 0 else " — fresh identity"))
            ax.set_xlabel("Seconds relative to " + ("window boundary" if row == 0 else "burst target"))
            ax.set_ylabel(("Accepted requests" if accepted_only else "Requests") + f" per {bucket_label} bucket")
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            ax.grid(axis="y", alpha=0.2)
            if not accepted_only:
                ax.text(0.98, 0.92, f"{len(arrived)} arrived / {len(items)} total",
                        transform=ax.transAxes, ha="right", va="top", fontsize=10)

            # Exact timestamp steps preserve bursts without smoothing.
            for times, style, color, series in ((offered, "--", "#6b7280", "Total attempted"),
                                               (arrived, "-", "#2563eb", "Arrived at upstream")):
                times = sorted(times)
                trend.step([left] + times + [right], [0] + list(range(1, len(times) + 1)) + [len(times)],
                           where="post", color=color, linestyle=style, linewidth=1.8, label=series)
            trend.set_title(label + (" — boundary bursts and recovery" if case == "boundary" else " — fresh identity"))
            trend.set_xlabel("Seconds relative to " + ("window boundary" if case == "boundary" else "burst target"))
            trend.set_ylabel("Cumulative requests")
            trend.set_xlim(left, right)
            trend.yaxis.set_major_locator(MaxNLocator(integer=True))
            trend.grid(alpha=0.2)
            if case == "boundary":
                window_seconds = data["window_ms"] / 1000
                for boundary in np.arange(0, right + 0.001, window_seconds):
                    trend.axvline(boundary, color="#374151", linestyle=":", linewidth=1, alpha=0.5)
                if data.get("duration_seconds", 0) > 10:
                    initial = [r for r in items if r["phase"] in ("before", "after")]
                    initial_arrived = sum(r["upstream_timestamp"] is not None for r in initial)
                    trend.annotate(f"Initial bursts: {initial_arrived}/{len(initial)} arrived",
                                   xy=(2, initial_arrived), xytext=(right * 0.2, initial_arrived + 15),
                                   arrowprops={"arrowstyle": "->", "color": "#4b5563"}, fontsize=9)
            trend.legend(loc="lower right", fontsize=9)
            for phase in sorted({r["phase"] for r in items}):
                group = [r for r in items if r["phase"] == phase]
                summary.append(f"| {case} | {algorithm} | {phase} | {len(group)} | "
                               f"{sum(r['upstream_timestamp'] is not None for r in group)} | "
                               f"{sum(r['return_code'] == 429 for r in group)} |")

    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncols=3, frameon=False)
    title = "Accepted request counts (not cumulative)" if accepted_only else "Traffic distribution from saved Parquet"
    fig.suptitle(f"{title} · {bucket_label} buckets\n"
                 f"{data['limit']} attempts / {data['window_ms'] / 1000:g}s · actual upstream arrivals", fontsize=14)
    trend_title = "Total vs upstream-arrived requests over time · saved Parquet"
    if data.get("duration_seconds"):
        trend_title += f"\n{data['limit']}/{data['window_ms'] / 1000:g}s per client; after the bursts, client sends 1 request/s"
    trends.suptitle(trend_title, fontsize=13)
    for figure, name in ((fig, "window-bursts"), (trends, "window-totals")):
        figure.savefig(output / (name + ".png"), dpi=180)
        figure.savefig(output / (name + ".svg"))
        plt.close(figure)
    text = f"# Offline traffic plots\n\nSource: `{path.name}`; {len(rows)} request rows; {bin_ms:g} ms buckets.\n\n"
    text += "| Case | Algorithm | Phase | Total | Arrived upstream | Returned 429 |\n| --- | --- | --- | ---: | ---: | ---: |\n" + "\n".join(summary)
    text += "\n\nDistribution Y axes show request counts per bucket, not requests/second. "
    text += "Cumulative plots use exact event timestamps. Returned-code counts are grouped by client send time; "
    text += "upstream counts use independently recorded ingress time. All timestamps are stored as UTC. "
    text += "A new identity can still burst; sliding windows retain weighted prior usage, not constant pacing.\n"
    if accepted_only:
        text += "\nThe distribution figure shows only accepted-request counts per bucket, not cumulative totals.\n"
    if data.get("duration_seconds"):
        text += (f"\nRecording: {data['duration_seconds']}s after the first boundary. Recovery traffic starts at +2.25s "
                 "and is deliberately sent by the client at 1 request/s per policy. "
                 "Its steady slope is an input schedule, not evidence of limiter pacing. "
                 "Dotted lines in the cumulative boundary panels mark subsequent window boundaries.\n")
    if data.get("missing_request_paths"):
        text += "\nLegacy data did not record request paths; these remain null in Parquet.\n"
    (output / "summary.md").write_text(text)
    (output / "plot-settings.json").write_text(json.dumps({"source": str(path.resolve()), "bin_ms": bin_ms,
                                                         "accepted_only": accepted_only,
                                                         "request_rows": len(rows)}, indent=2) + "\n")
    if any(r["scenario"] == "retry-follow" for r in rows):
        plot_retries(data, rows, output)
    print(f"Plotted {len(rows)} saved requests to {output}; no traffic generated.")



def plot_retries(data, rows, output):
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), sharex=True, sharey=True, layout="constrained")
    reference = data["retry_start_ms"] * 1_000_000
    duration = (data["boundary_ms"] + data["duration_seconds"] * 1000 - data["retry_start_ms"]) / 1000
    retry_rows = [r for r in rows if r["scenario"] in ("retry-ignore", "retry-follow")]
    # Requests sent before the deadline can arrive upstream after it.
    offsets = [(r[field] - reference) / 1e9 for r in retry_rows
               for field in ("timestamp", "upstream_timestamp") if r[field] is not None]
    right = max(duration, max(offsets, default=0))
    bins = np.arange(0, math.ceil(right) + 1)
    for row, behavior in enumerate(("retry-ignore", "retry-follow")):
        for col, algorithm in enumerate(("fixed_window", "sliding_window")):
            ax = axes[row, col]
            items = [r for r in rows if r["scenario"] == behavior and r["algorithm"] == algorithm]
            for field, status, color, label in (("timestamp", None, "#6b7280", "Attempted"),
                                               ("upstream_timestamp", 200, "#2563eb", "Accepted at upstream"),
                                               ("timestamp", 429, "#dc2626", "Returned 429")):
                values = [(r[field] - reference) / 1e9 for r in items
                          if r[field] is not None and (status is None or r["return_code"] == status)]
                counts, _ = np.histogram(values, bins)
                assert counts.sum() == len(values)
                ax.stairs(counts, bins, color=color, label=label, linewidth=1.5)
            accepted = sum(r["return_code"] == 200 for r in items)
            ax.set_title(f"{algorithm.removesuffix('_window').title()} — " +
                         ("ignores Retry-After" if row == 0 else "respects Retry-After"))
            ax.text(.98, .93, f"{accepted} accepted / {len(items)} attempts", transform=ax.transAxes,
                    ha="right", va="top", fontsize=10)
            ax.set_xlabel("Seconds since identical initial overload")
            ax.set_ylabel("Requests per 1 s bucket")
            ax.set_xlim(0, right)
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            ax.grid(axis="y", alpha=.2)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncols=3, frameon=False)
    fig.suptitle(f"Client retry behavior · all attempts count · {data['limit']}/{data['window_ms']/1000:g}s per identity\n"
                 "Same initial limit+1 attempts; then 5 requests/s, with pauses for the cooperative client")
    for suffix in ("png", "svg"):
        fig.savefig(output / ("retry-behavior." + suffix), dpi=180)
    plt.close(fig)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("parquet", type=Path)
    parser.add_argument("--bin-ms", type=float, default=100, help="Distribution bucket width in milliseconds")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--boundary-seconds", type=float, help="Crop boundary case to this many seconds after reset")
    parser.add_argument("--accepted-only", action="store_true", help="Show accepted counts only in the distribution figure")
    args = parser.parse_args()
    if not math.isfinite(args.bin_ms) or args.bin_ms < 1:
        parser.error("bin-ms must be finite and at least 1")
    if args.boundary_seconds is not None and (not math.isfinite(args.boundary_seconds) or args.boundary_seconds <= 1):
        parser.error("boundary-seconds must be finite and greater than 1")
    plot(args.parquet, args.output_dir or args.parquet.parent, args.bin_ms, args.accepted_only, args.boundary_seconds)
