"""Create the canonical JSONL submission from dataset/expanded/test_pairs.json."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot import compose


def load_by_id(directory: Path, field: str) -> dict[str, dict]:
    result = {}
    for path in directory.glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        result[str(item[field])] = item
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="dataset/expanded", help="Expanded challenge dataset directory")
    parser.add_argument("--out", default="submission.jsonl", help="Output JSONL path")
    args = parser.parse_args()
    root = Path(args.dataset)
    categories = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (root / "categories").glob("*.json")}
    merchants = load_by_id(root / "merchants", "merchant_id")
    customers = load_by_id(root / "customers", "customer_id")
    triggers = load_by_id(root / "triggers", "id")
    pairs = json.loads((root / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]

    output = []
    for pair in pairs:
        trigger = triggers[pair["trigger_id"]]
        merchant = merchants[pair["merchant_id"]]
        category = categories[merchant["category_slug"]]
        customer_id = pair.get("customer_id") or trigger.get("customer_id")
        customer = customers.get(customer_id) if customer_id else None
        result = compose(category, merchant, trigger, customer)
        output.append({
            "test_id": pair["test_id"],
            "body": result["body"],
            "cta": result["cta"],
            "send_as": "merchant_on_behalf" if customer else "vera",
            "suppression_key": trigger.get("suppression_key", ""),
            "rationale": result["rationale"],
        })
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output), encoding="utf-8")
    print(f"Wrote {len(output)} challenge responses to {out}")


if __name__ == "__main__":
    main()
