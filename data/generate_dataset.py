"""Synthetic multi-source financial dataset generator.

Produces three independent, messy source files that must be reconciled:
  - payments.csv          (payment gateway transactions -- clean/authoritative)
  - bank_settlements.csv  (bank/settlement statement -- messy narrations, delays)
  - invoices.csv          (internal ledger/ERP invoices -- formal naming)

...plus ground_truth.csv, which is NEVER read by the reconciliation engine.
It exists solely for evaluation: for every payment it records the true
matching bank record (if any), the true invoice (if any), a human-readable
case_type used for demo narration, and `is_safely_resolvable` -- whether a
uniquely correct answer is actually determinable from the evidence at all.

Determinism: everything is derived from a single `random.Random(seed)`
instance and a fixed anchor date, so re-running with the same seed/size
reproduces byte-identical output.

Usage:
    python data/generate_dataset.py --seed 42 --size 750 --out-dir data/raw
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import string
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ANCHOR_DATE = datetime(2026, 8, 1, 9, 0, 0)  # fixed "today" for the synthetic world

# ---------------------------------------------------------------------------
# Name pools
# ---------------------------------------------------------------------------

LEGAL_SUFFIXES = [
    ("Private Limited", "Pvt Ltd", "Pvt. Ltd.", "P Ltd", "(P) Ltd"),
]

COMPANY_BASES = [
    "Acme Retail", "Nimbus Cloud", "Sunrise Textiles", "Bluepeak Logistics",
    "Ganges Foods", "Silverline Motors", "Everest Analytics", "Coral Bay Hotels",
    "Vertex Pharma", "Northstar Media", "Indus Valley Traders", "Zenith Steel",
    "Orchid Interiors", "Falcon Freight", "Marigold Organics", "Crestline Fintech",
    "Amber Leaf Tea", "Granite Builders", "Skyline Apparel", "Riverbend Dairy",
    "Quantum Devices", "Pioneer Agro", "Lotus Handicrafts", "Titan Auto Parts",
    "Meridian Consulting", "Copperfield Hardware", "Emerald Exports", "Basil Foods",
    "Highline Realty", "Cobalt Electronics", "Windsor Furnishings", "Palmgrove Resorts",
    "Ashoka Chemicals", "Bright Future Edutech", "Cedar Ridge Interiors", "Delta Freight Movers",
]

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Krishna",
    "Ishaan", "Rohan", "Kabir", "Aryan", "Dhruv", "Karan", "Nikhil", "Rahul",
    "Ananya", "Diya", "Priya", "Isha", "Kavya", "Meera", "Neha", "Pooja",
    "Riya", "Sanya", "Tanvi", "Ritu", "Sneha", "Anjali", "Divya", "Shreya",
]

LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Iyer", "Nair", "Reddy", "Rao", "Menon",
    "Patel", "Shah", "Mehta", "Kapoor", "Malhotra", "Chatterjee", "Banerjee",
    "Kulkarni", "Joshi", "Desai", "Pillai", "Agarwal",
]

BANK_CODES = ["hdfc", "icic", "sbi", "axis", "kotak", "ybl", "okaxis", "oksbi"]
METHODS = ["upi", "card", "netbanking", "wallet"]

CASE_WEIGHTS: list[tuple[str, float]] = [
    ("exact_match", 0.38),
    ("name_variation", 0.10),
    ("abbreviation", 0.05),
    ("formatting_diff", 0.05),
    ("settlement_delay", 0.08),
    ("reference_variation", 0.06),
    ("duplicate_bank_record", 0.04),
    ("amount_mismatch_small", 0.03),
    ("amount_mismatch_large", 0.03),
    ("missing_bank_record", 0.04),
    ("missing_invoice", 0.04),
    ("missing_fields", 0.04),
    ("ambiguous_multiple_candidates", 0.04),
    ("conflicting_evidence", 0.02),
]

# Cases where NO system (human or AI) can determine a uniquely correct
# reconciliation from the available evidence alone. These are the cases
# that MUST be escalated, never auto-matched, for the system to be "safe".
UNRESOLVABLE_CASES = {
    "amount_mismatch_large",
    "missing_bank_record",
    "ambiguous_multiple_candidates",
    "conflicting_evidence",
}


def token(rng: random.Random, n: int, alphabet: str = string.ascii_uppercase + string.digits) -> str:
    return "".join(rng.choices(alphabet, k=n))


def pick_amount(rng: random.Random) -> float:
    if rng.random() < 0.35:
        base = rng.choice([499, 999, 1499, 1999, 2999, 4999, 9999, 14999, 24999, 49999, 99999])
        return float(base)
    return round(rng.uniform(350, 225000), 2)


@dataclass
class Company:
    legal_name: str
    short_name: str
    suffix_variants: tuple

    def variant(self, rng: random.Random, kind: str) -> str:
        if kind == "canonical":
            return self.legal_name
        if kind == "abbreviation":
            words = self.short_name.split()
            if len(words) >= 2:
                return "".join(w[0] for w in words).upper()
            return self.short_name.upper()[:4]
        if kind == "alt_suffix":
            return f"{self.short_name} {rng.choice(self.suffix_variants)}"
        if kind == "upper_no_punct":
            return "".join(c for c in self.legal_name.upper() if c.isalnum() or c == " ")
        return self.legal_name


def build_companies() -> list[Company]:
    out = []
    for base in COMPANY_BASES:
        suffixes = LEGAL_SUFFIXES[0]
        out.append(Company(legal_name=f"{base} {suffixes[0]}", short_name=base, suffix_variants=suffixes[1:]))
    return out


def build_persons(rng: random.Random) -> list[str]:
    seen = set()
    people = []
    while len(people) < 80:
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        if name not in seen:
            seen.add(name)
            people.append(name)
    return people


@dataclass
class Entity:
    canonical_name: str
    kind: str  # "company" | "person"
    company: Optional[Company] = None


def build_entities(rng: random.Random) -> list[Entity]:
    entities = [Entity(c.legal_name, "company", c) for c in build_companies()]
    entities += [Entity(p, "person") for p in build_persons(rng)]
    rng.shuffle(entities)
    return entities


def name_variant(entity: Entity, rng: random.Random, kind: str) -> str:
    if entity.kind == "company":
        return entity.company.variant(rng, kind)
    # person name variants
    parts = entity.canonical_name.split()
    first, last = parts[0], parts[-1]
    if kind == "canonical":
        return entity.canonical_name
    if kind == "abbreviation":
        return f"{first[0]}. {last}"
    if kind == "alt_suffix":  # reordered / casing variant for persons
        return f"{last} {first}".upper()
    if kind == "upper_no_punct":
        return entity.canonical_name.upper()
    return entity.canonical_name


def normalize_ref(s: str) -> str:
    return "".join(c for c in s.upper() if c.isalnum())


@dataclass
class GroundTruthRow:
    payment_id: str
    true_bank_ref: str
    true_invoice_id: str
    case_type: str
    is_safely_resolvable: bool
    notes: str


def generate(seed: int, size: int, out_dir: Path) -> dict:
    rng = random.Random(seed)
    entities = build_entities(rng)

    payments: list[dict] = []
    banks: list[dict] = []
    invoices: list[dict] = []
    ground_truth: list[GroundTruthRow] = []

    case_types = [c for c, _ in CASE_WEIGHTS]
    weights = [w for _, w in CASE_WEIGHTS]

    # pool of (entity, generic bank narration style) used to build genuinely
    # ambiguous clusters -- created up front by reserving a slice of payments
    ambiguous_cluster_budget = max(2, int(size * dict(CASE_WEIGHTS)["ambiguous_multiple_candidates"]))
    pending_ambiguous: list[dict] = []

    for i in range(size):
        case_type = rng.choices(case_types, weights=weights, k=1)[0]
        entity = rng.choice(entities)

        payment_id = f"pay_{token(rng, 14)}"
        order_token = token(rng, 12)
        order_id = f"order_{order_token}"
        amount = pick_amount(rng)
        created_at = ANCHOR_DATE - timedelta(
            days=rng.randint(1, 60), hours=rng.randint(0, 23), minutes=rng.randint(0, 59)
        )
        method = rng.choice(METHODS)
        email_local = entity.canonical_name.lower().replace(" ", ".")[:20]
        email = f"{email_local}@example.com"
        description = f"Order payment - {order_id}"

        payment_row = {
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "currency": "INR",
            "method": method,
            "customer_name": entity.canonical_name,
            "customer_email": email,
            "created_at": created_at.isoformat(),
            "status": "captured",
            "description": description,
        }

        utr = f"UTR{token(rng, 12, string.digits)}"
        invoice_id = f"INV-{created_at.year}-{rng.randint(10000, 99999)}"

        bank_amount = amount
        bank_delay_days = rng.choice([0, 1, 1, 2])
        payer_name_kind = "canonical"
        ref_in_narration = order_token
        skip_bank = False
        skip_invoice = False
        extra_duplicate_bank = False
        blank_field: Optional[str] = None
        note = ""
        resolvable = True

        if case_type == "exact_match":
            note = "Clean match: exact reference, exact amount, exact name, fast settlement."

        elif case_type == "name_variation":
            payer_name_kind = rng.choice(["alt_suffix", "upper_no_punct"])
            note = f"Bank narration uses a name variant ({payer_name_kind}) of the customer name; reference and amount are clean."

        elif case_type == "abbreviation":
            payer_name_kind = "abbreviation"
            note = "Bank narration abbreviates the customer name; reference and amount are clean."

        elif case_type == "formatting_diff":
            ref_in_narration = order_token.lower()
            note = "Reference formatting differs in case/punctuation but is otherwise intact."

        elif case_type == "settlement_delay":
            bank_delay_days = rng.randint(3, 7)
            note = f"Settlement delayed by {bank_delay_days} days; still within the settlement window."

        elif case_type == "reference_variation":
            ref_in_narration = order_token[-6:]  # only trailing fragment retained
            payer_name_kind = rng.choice(["alt_suffix", "canonical"])
            note = "Reference is truncated/garbled in the bank narration; name+amount+date must corroborate."

        elif case_type == "duplicate_bank_record":
            extra_duplicate_bank = True
            note = "Bank posted a duplicate settlement row for the same transaction (double posting)."

        elif case_type == "amount_mismatch_small":
            bank_amount = round(amount * (1 - rng.uniform(0.005, 0.02)), 2)  # fee-like deduction
            note = f"Bank amount is {amount - bank_amount:.2f} lower than payment amount (looks like a fee deduction)."

        elif case_type == "amount_mismatch_large":
            bank_amount = round(amount * rng.uniform(1.15, 1.6), 2)
            note = "Bank amount differs by >15% with no plausible fee/tax explanation -- must NOT be auto-matched."
            resolvable = False

        elif case_type == "missing_bank_record":
            skip_bank = True
            note = "Payment was captured by the gateway but never appears in the bank settlement file."
            resolvable = False

        elif case_type == "missing_invoice":
            skip_invoice = True
            note = "Payment settled correctly but no corresponding invoice exists in the ledger."

        elif case_type == "missing_fields":
            blank_field = rng.choice(["bank_payer_name", "customer_email", "invoice_customer_name"])
            if rng.random() < 0.3:
                blank_field = "payment_amount_corrupt"
                resolvable = False
                note = "Payment amount field is corrupt/missing -- cannot be reconciled without it."
            else:
                note = f"A non-essential field ({blank_field}) is missing; remaining evidence should still be enough."

        elif case_type == "ambiguous_multiple_candidates":
            note = "Two near-identical payments/settlements from the same customer with no distinguishing reference."
            resolvable = False
            pending_ambiguous.append({
                "payment_id": payment_id, "order_id": order_id, "amount": amount,
                "created_at": created_at, "entity": entity, "payment_row": payment_row,
            })

        elif case_type == "conflicting_evidence":
            payer_name_kind = "canonical"
            # exact reference match but wildly different amount+slightly different name -> conflict
            bank_amount = round(amount * rng.uniform(1.4, 2.2), 2)
            note = "Reference matches exactly but amount is wildly different -- conflicting evidence, must not auto-resolve."
            resolvable = False

        payments.append(payment_row)

        true_bank_refs: list[str] = []
        true_invoice_ids: list[str] = []

        if not skip_bank and case_type != "ambiguous_multiple_candidates":
            settlement_date = (created_at + timedelta(days=bank_delay_days)).date()
            payer_display = name_variant(entity, rng, payer_name_kind)
            narration_style = rng.choice([
                f"NEFT/{utr}/{payer_display.upper()}/{ref_in_narration}",
                f"UPI-{payer_display.upper()}-{rng.randint(6000000000,9999999999)}@{rng.choice(BANK_CODES)}-{ref_in_narration}",
                f"IMPS/{utr}/{payer_display}/{ref_in_narration}",
                f"{payer_display.upper()}-{ref_in_narration}-SETTLEMENT",
                f"RTGS {utr} {payer_display.upper()} {ref_in_narration}",
            ])
            bank_row = {
                "bank_ref": f"bnk_{token(rng, 12)}",
                "utr": utr,
                "settlement_date": settlement_date.isoformat(),
                "amount": bank_amount,
                "narration": narration_style,
                "payer_name": "" if blank_field == "bank_payer_name" else payer_display,
                "reference_hint": ref_in_narration,
            }
            banks.append(bank_row)
            true_bank_refs.append(bank_row["bank_ref"])

            if extra_duplicate_bank:
                dup_row = dict(bank_row)
                dup_row["bank_ref"] = f"bnk_{token(rng, 12)}"
                dup_settle = settlement_date + timedelta(days=rng.choice([0, 1]))
                dup_row["settlement_date"] = dup_settle.isoformat()
                banks.append(dup_row)
                true_bank_refs.append(dup_row["bank_ref"])

        if not skip_invoice:
            inv_customer_kind = "canonical" if entity.kind == "person" else "canonical"
            inv_name = "" if blank_field == "invoice_customer_name" else entity.canonical_name
            invoice_amount = amount if case_type != "amount_mismatch_small" else amount
            invoice_row = {
                "invoice_id": invoice_id,
                "order_id": order_id,
                "amount": invoice_amount,
                "customer_name": inv_name,
                "invoice_date": (created_at + timedelta(days=rng.randint(0, 2))).date().isoformat(),
                "description": f"Invoice for {order_id}",
                "status": "paid",
            }
            invoices.append(invoice_row)
            true_invoice_ids.append(invoice_id)

        if blank_field == "customer_email":
            payment_row["customer_email"] = ""
        if blank_field == "payment_amount_corrupt":
            payment_row["amount"] = ""

        if case_type != "ambiguous_multiple_candidates":
            ground_truth.append(GroundTruthRow(
                payment_id=payment_id,
                true_bank_ref="|".join(true_bank_refs),
                true_invoice_id="|".join(true_invoice_ids),
                case_type=case_type,
                is_safely_resolvable=resolvable,
                notes=note,
            ))

    # --- build ambiguous clusters: pair up pending_ambiguous payments so that
    # each pair shares near-identical amount/date/customer with generic bank
    # narrations, producing genuinely indistinguishable candidates.
    rng.shuffle(pending_ambiguous)
    for j in range(0, len(pending_ambiguous) - 1, 2):
        p1, p2 = pending_ambiguous[j], pending_ambiguous[j + 1]
        shared_entity = p1["entity"]
        shared_amount = p1["amount"]
        base_date = p1["created_at"]
        # Align p2's payment timestamp to p1's so both bank candidates fall
        # within BOTH payments' settlement windows -- otherwise the pair is
        # not actually ambiguous from one side's point of view.
        p2["created_at"] = base_date
        p2["payment_row"]["created_at"] = base_date.isoformat()
        for p in (p1, p2):
            utr = f"UTR{token(rng, 12, string.digits)}"
            settle_date = (base_date + timedelta(days=rng.choice([1, 2]))).date()
            payer_display = name_variant(shared_entity, rng, "canonical")
            narration = f"UPI-{payer_display.upper()}-{rng.randint(6000000000,9999999999)}@{rng.choice(BANK_CODES)}"
            bank_row = {
                "bank_ref": f"bnk_{token(rng, 12)}",
                "utr": utr,
                "settlement_date": settle_date.isoformat(),
                "amount": shared_amount,
                "narration": narration,
                "payer_name": payer_display,
                "reference_hint": "",
            }
            banks.append(bank_row)
            inv_row = {
                "invoice_id": f"INV-{base_date.year}-{rng.randint(10000, 99999)}",
                "order_id": p["order_id"],
                "amount": shared_amount,
                "customer_name": shared_entity.canonical_name,
                "invoice_date": base_date.date().isoformat(),
                "description": f"Invoice for {p['order_id']}",
                "status": "paid",
            }
            invoices.append(inv_row)
            ground_truth.append(GroundTruthRow(
                payment_id=p["payment_id"],
                true_bank_ref="AMBIGUOUS",
                true_invoice_id=inv_row["invoice_id"],
                case_type="ambiguous_multiple_candidates",
                is_safely_resolvable=False,
                notes="Two same-customer, same-amount, same-window payments with generic UPI narrations; "
                      "no reliable evidence distinguishes which bank row belongs to which payment.",
            ))
    if len(pending_ambiguous) % 2 == 1:
        # odd one out: treat as a normal exact match rather than discard it
        p = pending_ambiguous[-1]
        entity = p["entity"]
        utr = f"UTR{token(rng, 12, string.digits)}"
        settle_date = (p["created_at"] + timedelta(days=1)).date()
        bank_row = {
            "bank_ref": f"bnk_{token(rng, 12)}", "utr": utr, "settlement_date": settle_date.isoformat(),
            "amount": p["amount"], "narration": f"NEFT/{utr}/{entity.canonical_name.upper()}/{p['order_id'][6:]}",
            "payer_name": entity.canonical_name, "reference_hint": p["order_id"][6:],
        }
        banks.append(bank_row)
        ground_truth.append(GroundTruthRow(
            payment_id=p["payment_id"], true_bank_ref=bank_row["bank_ref"], true_invoice_id="",
            case_type="exact_match", is_safely_resolvable=True, notes="Leftover paired payment resolved as a clean match.",
        ))

    rng.shuffle(banks)
    rng.shuffle(invoices)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "payments.csv", payments,
               ["payment_id", "order_id", "amount", "currency", "method", "customer_name",
                "customer_email", "created_at", "status", "description"])
    _write_csv(out_dir / "bank_settlements.csv", banks,
               ["bank_ref", "utr", "settlement_date", "amount", "narration", "payer_name", "reference_hint"])
    _write_csv(out_dir / "invoices.csv", invoices,
               ["invoice_id", "order_id", "amount", "customer_name", "invoice_date", "description", "status"])
    _write_csv(out_dir / "ground_truth.csv",
               [gt.__dict__ for gt in ground_truth],
               ["payment_id", "true_bank_ref", "true_invoice_id", "case_type", "is_safely_resolvable", "notes"])

    summary = {
        "seed": seed,
        "payments": len(payments),
        "bank_settlements": len(banks),
        "invoices": len(invoices),
        "case_type_counts": {ct: sum(1 for g in ground_truth if g.case_type == ct) for ct in case_types},
        "resolvable": sum(1 for g in ground_truth if g.is_safely_resolvable),
        "unresolvable": sum(1 for g in ground_truth if not g.is_safely_resolvable),
    }
    with open(out_dir / "generation_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    return summary


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic multi-source reconciliation dataset")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--size", type=int, default=750)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).parent / "raw")
    args = parser.parse_args()

    summary = generate(args.seed, args.size, args.out_dir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
