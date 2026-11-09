"""Export captured JSON to Parquet without starting Docker or generating traffic.

uv run --no-project --with pyarrow==22.0.0 python test/export_window_parquet.py /tmp/capture/arrivals.json
"""
import argparse
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def export(source, target):
    data = json.loads(source.read_text())
    received = {event["request_id"]: event["received_ns"] for event in data["upstream_arrivals"]}
    assert len(received) == len(data["upstream_arrivals"]), "Duplicate upstream request IDs"
    attempts = data["attempts"]
    assert len({a["request_id"] for a in attempts}) == len(attempts), "Duplicate client request IDs"
    assert {a["request_id"] for a in attempts if a["status"] == 200} == set(received), \
        "Upstream arrivals must exactly match successful client requests"
    rows = [{"timestamp": a["sent_ns"], "return_code": a["status"],
             "request_path": a.get("request_path"), "response_timestamp": a.get("response_ns"),
             "upstream_timestamp": received.get(a["request_id"]), "kong_timestamp": a["kong_ms"],
             "request_id": a["request_id"], "algorithm": a["algorithm"],
             "scenario": a.get("scenario", "fresh" if a["phase"] == "idle-burst" else "boundary"),
             "retry_after_seconds": a.get("retry_after_seconds"),
             "phase": a["phase"], "gateway": a["gateway"]} for a in attempts]
    rows.sort(key=lambda row: (row["timestamp"], row["request_id"]))
    metadata = {key: value for key, value in data.items() if key not in ("attempts", "upstream_arrivals")}
    metadata.update({"format_version": 2, "timestamp_semantics": "Client request send time, UTC",
                     "upstream_timestamp_semantics": "Actual upstream ingress time, UTC; null when not forwarded",
                     "missing_request_paths": sum(row["request_path"] is None for row in rows),
                     "missing_response_timestamps": sum(row["response_timestamp"] is None for row in rows)})
    schema = pa.schema([
        pa.field("timestamp", pa.timestamp("ns", tz="UTC"), nullable=False),
        pa.field("return_code", pa.uint16(), nullable=False),
        pa.field("request_path", pa.string()),
        pa.field("response_timestamp", pa.timestamp("ns", tz="UTC")),
        pa.field("upstream_timestamp", pa.timestamp("ns", tz="UTC")),
        pa.field("kong_timestamp", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("retry_after_seconds", pa.uint32()),
        pa.field("request_id", pa.string(), nullable=False),
        pa.field("algorithm", pa.string(), nullable=False),
        pa.field("scenario", pa.string(), nullable=False),
        pa.field("phase", pa.string(), nullable=False),
        pa.field("gateway", pa.string(), nullable=False),
    ], metadata={b"kong.window_illustration": json.dumps(metadata).encode()})
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), target, compression="zstd")
    print(f"Wrote {len(rows)} requests to {target}")
    if metadata["missing_request_paths"]:
        print("Legacy capture did not record request paths; those values remain null, not inferred.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    export(args.capture, args.output or args.capture.with_name("requests.parquet"))
