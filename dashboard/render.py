"""Build a single, local-file-friendly offline dashboard with no external assets."""
import base64
import gzip
import json
from pathlib import Path


def render(payload):
    root = Path(__file__).parent
    source_ids = {s["source_id"] for s in payload["sources"]}
    cases = json.loads(root.joinpath("case_notes.json").read_text(encoding="utf-8"))
    notes = {p: note for p, note in cases.items() if note["source_id"] in source_ids}
    metrics = json.loads((root.parent / "config/metrics.json").read_text(encoding="utf-8"))
    metric_labels = {k: definition[0] for k, definition in metrics.items()}
    data = json.dumps(dict(payload, case_notes=notes, metric_labels=metric_labels), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    packed = base64.b64encode(gzip.compress(data.encode("utf-8"), mtime=0)).decode("ascii")
    return (root.joinpath("layout.html").read_text(encoding="utf-8")
            .replace("/*__STYLE__*/", root.joinpath("style.css").read_text(encoding="utf-8"))
            .replace("/*__SCRIPT__*/", root.joinpath("viewer.js").read_text(encoding="utf-8"))
            .replace("__PACKED_DATA__", packed))
