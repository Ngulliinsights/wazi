"""Operator CLI: load data, audit it, review and approve filings (human side of the gate)."""
import argparse
import json

from . import config, gate
from .store import connect, load_ocds


def audit(conn) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]
    n = q("SELECT COUNT(*) FROM tenders") or 1
    pct = lambda c: round(100 * q(f"SELECT COUNT(*) FROM tenders WHERE {c}") / n, 1)
    amts = sorted((r[0] for r in conn.execute("SELECT amount FROM awards WHERE amount>0")), reverse=True)
    top = max(1, len(amts) // 100)
    return {"tenders": n, "awards": q("SELECT COUNT(*) FROM awards"),
            "pct_with_deadline": pct("deadline IS NOT NULL"),
            "pct_with_eligibility": pct("eligibility IS NOT NULL AND eligibility<>''"),
            "pct_with_buyer_name": pct("buyer_name IS NOT NULL AND buyer_name<>''"),
            "pct_tenders_with_award": round(100 * q("SELECT COUNT(DISTINCT ocid) FROM awards") / n, 1),
            "awards_missing_supplier": q("SELECT COUNT(*) FROM awards WHERE supplier_id IS NULL AND supplier_name IS NULL"),
            "top1pct_share_of_award_value": round(sum(amts[:top]) / sum(amts), 3) if amts else None}


def main():
    p = argparse.ArgumentParser(prog="wazi")
    s = p.add_subparsers(dest="cmd", required=True)
    l = s.add_parser("load"); l.add_argument("path", nargs="?", default=str(config.SAMPLE_DATA))
    l.add_argument("--synthetic", action="store_true", help="mark this data as synthetic (exempt from the live-tender rule)"); l.add_argument("--real", action="store_true", help="force: treat as real data even if the publisher says synthetic")
    s.add_parser("audit")
    s.add_parser("pending")
    sh = s.add_parser("show"); sh.add_argument("filing_id")
    vf = s.add_parser("verify"); vf.add_argument("filing_id")
    a = s.add_parser("approve"); a.add_argument("filing_id"); a.add_argument("--approver", required=True); a.add_argument("--sha256", required=True, help="fingerprint printed by `show`"); a.add_argument("--role", required=True, choices=sorted(gate.VALID_ROLES)); a.add_argument("--date", required=True, help="approval date (YYYY-MM-DD)")
    r = s.add_parser("reject"); r.add_argument("filing_id"); r.add_argument("--approver", required=True); r.add_argument("--reason", default=""); r.add_argument("--sha256", required=True); r.add_argument("--role", required=True, choices=sorted(gate.VALID_ROLES)); r.add_argument("--date", required=True, help="approval date (YYYY-MM-DD)")
    args = p.parse_args()
    try:
        _run(args)
    except (PermissionError, FileNotFoundError, gate.ConflictError) as e:
        print(f"ERROR: {e}"); raise SystemExit(1)


def _run(args):
    if args.cmd == "load":
        print(json.dumps(load_ocds(args.path, synthetic=True if args.synthetic else (False if args.real else None)), indent=2))
    elif args.cmd == "audit":
        print(json.dumps(audit(connect()), indent=2))
    elif args.cmd == "pending":
        print(json.dumps(gate.list_pending(), indent=2))
    elif args.cmd == "show":
        md = next((config.OUTBOX / d / f"{args.filing_id}.md" for d in ("pending", "filed", "rejected") if (config.OUTBOX / d / f"{args.filing_id}.md").exists()), None)
        print(md.read_text(encoding="utf-8") if md else "no such filing"); print(f"\nBRIEF FINGERPRINT (sha256): {gate.brief_sha256(args.filing_id)}")
    elif args.cmd == "verify":
        from . import provenance
        rep = provenance.verify_filing(args.filing_id)
        for c in rep["checks"]:
            print(("PASS  " if c["ok"] else "FAIL  ") + c["check"] + (f"   <- {c['detail']}" if c["detail"] and not c["ok"] else ""))
        print("\nVERDICT:", rep["verdict"])
    elif args.cmd == "approve":
        print(json.dumps(gate.approve(args.filing_id, args.approver, args.sha256, args.role, args.date), indent=2))
    elif args.cmd == "reject":
        print(json.dumps(gate.reject(args.filing_id, args.approver, args.reason, args.sha256, args.role, args.date), indent=2))


if __name__ == "__main__":
    main()
