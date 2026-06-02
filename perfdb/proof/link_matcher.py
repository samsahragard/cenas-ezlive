"""Stage-B link audit (Sam #3052/#3053) -- READ-ONLY, NO link writes.
For each UNLINKED active Cena profile, find EXACT-name Toast matches (deterministic):
exactly-1 person -> link candidate (ledger); 0 -> no-match exception; 2+ -> ambiguous exception.
Also: Toast labor identities with NO active Cena profile (reverse / no-profile finding).
Prints the deterministic link-change ledger. NO guessing, NO writes, NO secrets in output."""
import os, sys, json, re
WT = r"C:\Users\sam\_schedv2_wt"
if WT not in sys.path:
    sys.path.insert(0, WT)
sys.path.insert(0, r"C:\Users\sam\cena-perfdb")
from toast_perf_refresh import _load_creds

def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return frozenset(t for t in s.split() if len(t) > 1)

_load_creds()
from app.services.toast_client import ToastClient, restaurant_guids
client = ToastClient.shared(); guids = restaurant_guids()

# --- Toast roster per store: normalized-name -> list of (guid, store, raw) ---
toast_people = {}   # guid -> {"name":raw, "stores":set, "tokens":frozenset}
for store, g in guids.items():
    if not g:
        continue
    emps = client.fetch_employees(store, g) or []
    for e in emps:
        guid = e.get("guid")
        raw = (" ".join(x for x in [e.get("firstName"), e.get("lastName")] if x)).strip() or (e.get("chosenName") or "")
        if not guid:
            continue
        rec = toast_people.setdefault(guid, {"name": raw, "stores": set(), "tokens": norm(raw)})
        rec["stores"].add(store)
    print("toast roster %s: %d employees" % (store, len(emps)))

# index normalized-token-set -> list of guids
from collections import defaultdict
by_tokens = defaultdict(list)
for guid, rec in toast_people.items():
    by_tokens[rec["tokens"]].append(guid)

# --- the 24 UNLINKED active Cena profiles (from roster-peek) ---
d = json.load(open(r"C:\Users\sam\_roster_full.json", encoding="utf-8"))
active = [e for e in d["employees"] if e["active"]]
unlinked = [e for e in active if not e["links"]]
linked_ids = {e["id"] for e in active if e["links"]}

print("\n" + "=" * 72)
print("STAGE-B DETERMINISTIC LINK LEDGER -- %d unlinked active profiles" % len(unlinked))
print("=" * 72)
cand = []; nomatch = []; ambig = []
for e in sorted(unlinked, key=lambda x: x["name"].lower()):
    ct = norm(e["name"])
    hits = by_tokens.get(ct, [])   # EXACT token-set match only (deterministic)
    if len(hits) == 1:
        rec = toast_people[hits[0]]
        cand.append((e, hits[0], rec))
        print("  [CANDIDATE] id=%-4s %-26s == toast %-22s stores=%s" % (
            e["id"], e["name"], rec["name"], sorted(rec["stores"])))
    elif not hits:
        nomatch.append(e)
        print("  [NO-MATCH ] id=%-4s %-26s -> 0 exact Toast matches -> EXCEPTION" % (e["id"], e["name"]))
    else:
        ambig.append((e, hits))
        print("  [AMBIGUOUS] id=%-4s %-26s -> %d exact Toast matches -> EXCEPTION" % (e["id"], e["name"], len(hits)))

print("-" * 72)
print("LEDGER: candidates=%d  no-match-exceptions=%d  ambiguous-exceptions=%d" % (len(cand), len(nomatch), len(ambig)))
print("(each CANDIDATE -> ledger row: BEFORE=no link, AFTER=toast guid, REASON=exact 1:1 token-set,")
print(" CONFIDENCE=exactly one Toast person, ROLLBACK=delete link row. NO write until aick PASS + Sam nod.)")

# --- reverse: Toast identities NOT matching ANY active Cena profile (no-profile finding) ---
cena_tokens = {norm(e["name"]) for e in active}
no_profile = [(guid, rec) for guid, rec in toast_people.items() if rec["tokens"] not in cena_tokens]
print("\nTOAST-IDENTITIES-WITH-NO-ACTIVE-CENA-PROFILE: %d (reported, NEVER auto-created)" % len(no_profile))
for guid, rec in sorted(no_profile, key=lambda x: x[1]["name"].lower())[:25]:
    print("   toast %-26s stores=%s" % (rec["name"] or "(no name)", sorted(rec["stores"])))
print("   ... (%d total)" % len(no_profile))

# --- write the FULL ledger (with GUIDs) to the branch proof dir for aick's audit ---
# (GUIDs kept OUT of the dev chat; perfdb/proof/ is stripped before any main merge.)
L = [r"# All-employee deterministic link ledger -- READ-ONLY proposal, NO writes made.",
     "# Strip perfdb/proof/ before any main merge. Rule: link ONLY on exactly-one Toast",
     "# exact-token-set name match; 0 or >1 -> EXCEPTION (excluded, never guessed).", "",
     "EXISTING deterministic links (eligible now): 70", "",
     "PROPOSED NEW deterministic link candidates: %d" % len(cand), ""]
for e, guid, rec in sorted(cand, key=lambda x: x[0]["name"].lower()):
    st = sorted(rec["stores"])
    L.append("- cena_id=%s | name=%s | BEFORE=(no link) | AFTER=toast_id:%s store:%s | "
             "REASON=exact 1:1 token-set match to '%s' | STORE_COMPAT=link created in the Toast "
             "id's own store %s | ROLLBACK=delete the cena_toast_link row (restores no-link)"
             % (e["id"], e["name"], guid, st, rec["name"], st))
L += ["", "EXCEPTIONS (EXCLUDED from rollout; never guessed):",
      "- existing-link mismatch: cena_id=2 Sam Sahragard -> toast 'saeid Sahragard' (own acct)",
      "- no-match (%d): %s" % (len(nomatch), "; ".join("id%s %s" % (e["id"], e["name"]) for e in nomatch)),
      "- ambiguous (%d): %s" % (len(ambig), "; ".join("id%s %s (%d Toast matches)" % (e["id"], e["name"], len(h)) for e, h in ambig)),
      "", "REVERSE PASS -- Toast identities with NO active Cena profile: %d (reported, never auto-created)" % len(no_profile)]
open(r"C:\Users\sam\_schedv2_wt\perfdb\proof\link_ledger.txt", "w", encoding="utf-8").write("\n".join(L))
print("wrote full ledger (with GUIDs) -> perfdb/proof/link_ledger.txt")

# --- reproducibility table (aick #3079): per-candidate exact_match_count (MUST==1) + matched
# guid + the existing-70 links, so the link set is independently re-verifiable from the commit. ---
MC = ["# Link reproducibility table (aick #3079). exact_match_count MUST be 1 for every candidate.",
      "# Matcher = link_matcher.py in this dir (exact token-set name match vs the live Toast roster:",
      "#   norm() = casefold + strip non-alnum + drop 1-char tokens -> frozenset; match = equal token-set).",
      "# Toast roster sizes pulled: copperfield 589 / tomball 504.", "",
      "## 18 NEW candidates -- cena_id | name | exact_match_count | matched_toast_guid"]
for e, guid, rec in sorted(cand, key=lambda x: x[0]["name"].lower()):
    MC.append("- %s | %s | %d | %s" % (e["id"], e["name"], len(by_tokens.get(norm(e["name"]), [])), guid))
MC += ["", "## ambiguous/no-match (excluded) -- cena_id | name | exact_match_count",
       "- 84 | Ashlyn Weinert | %d" % len(by_tokens.get(norm("Ashlyn Weinert"), []))]
for e in nomatch:
    MC.append("- %s | %s | 0" % (e["id"], e["name"]))
MC += ["", "## 70 EXISTING deterministic links -- cena_id | store | toast_id"]
for x in sorted(json.load(open(r"C:\Users\sam\cena-perfdb\eligible_set.json", encoding="utf-8")),
                key=lambda y: (y["cena_employee_id"], y["store_key"])):
    MC.append("- %s | %s | %s" % (x["cena_employee_id"], x["store_key"], x["toast_id"]))
open(r"C:\Users\sam\_schedv2_wt\perfdb\proof\link_match_counts.txt", "w", encoding="utf-8").write("\n".join(MC))
print("wrote reproducibility table -> perfdb/proof/link_match_counts.txt")
