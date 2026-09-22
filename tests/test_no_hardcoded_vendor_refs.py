"""ADR 016 addendum (2026-09-22) Decision 5, check 1 of 2: a static scan for
hardcoded vendor-account resource identities in `src/`.

Decision 1's whole claim — "telemetry-off falls out of deployment locality for
free" for a future Sovereign (in-account) deployment — is only true if every
module reads its resources from config (env vars) rather than a literal vendor
account id / bucket / table name baked into the code. One violation of that was
found live (Finding 0.4: `src/synth/lambda_handler.py`'s debug path defaulted to
the vendor's own digests bucket) and fixed alongside this test, so the test both
guards the fix and stands as the permanent version of the one-time audit —
catching the NEXT occurrence at review time instead of at a future Sovereign
tenant's deploy.

This is check 1 of 2 from the addendum's Decision 5. Check 2 (a synth-time
egress assertion on the tenant CDK stack — "the only thing crossing the account
boundary is the one governed /resolve call") lands with the tenant stack itself,
once it has Lambdas whose IAM grants there are something to assert about.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# The account id/resource names this scan treats as vendor-owned. Expand this
# list, don't work around it, if a new vendor-account identifier ever appears.
VENDOR_IDENTIFIERS = (
    "668449743071",  # the vendor AWS account id
    "onca-digests-668449743071",
    "onca-raw-668449743071",
    "oncaprototypestack-oncadashboardsitefdbca924-eu9minmw6ljv",
    "d37aa8gtuqquoe.cloudfront.net",
)

# A reference inside a comment explaining/forbidding the pattern (like this file,
# or the Finding-0.4 fix itself) is fine — only a reference reachable as CODE
# (i.e. not on a line starting with a comment marker once stripped) is a finding.
_COMMENT_PREFIXES = ("#",)


def _code_lines(path: Path):
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(_COMMENT_PREFIXES):
            continue
        # Strip a trailing inline comment so `x = 1  # mentions 668449743071` is
        # correctly treated as a comment reference, not a code one.
        code_part = re.split(r"(?<!['\"])#", raw, maxsplit=1)[0]
        if code_part.strip():
            yield lineno, code_part


def _scan() -> list[tuple[Path, int, str]]:
    hits = []
    for py_file in SRC.rglob("*.py"):
        for lineno, code in _code_lines(py_file):
            for ident in VENDOR_IDENTIFIERS:
                if ident in code:
                    hits.append((py_file.relative_to(REPO_ROOT), lineno, ident))
    return hits


def test_no_module_hardcodes_a_vendor_account_resource_identity():
    hits = _scan()
    assert not hits, (
        "Hardcoded vendor-account resource reference(s) found in src/ — a future "
        "Sovereign (in-account) deployment running this code would either try to "
        "reach the VENDOR's own resource (a cross-account failure at best, a leak "
        "at worst) or silently depend on an identifier that only means something "
        "in the vendor's account. Read it from config/env instead. See ADR 016 "
        "addendum, Finding 0.4 and Decision 5.\n" + "\n".join(
            f"  {path}:{lineno}  ({ident})" for path, lineno, ident in hits
        )
    )


def test_the_scan_itself_actually_catches_the_pattern_it_guards_against():
    # A meta-test: prove the scanner isn't a no-op by feeding it the exact
    # violation that was found and fixed (Finding 0.4), against a throwaway file.
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir=SRC, delete=False, encoding="utf-8"
    ) as f:
        f.write('BUCKET = os.environ.get("X") or "onca-digests-668449743071"\n')
        probe_path = Path(f.name)
    try:
        hits = [h for h in _scan() if h[0] == probe_path.relative_to(REPO_ROOT)]
        assert hits, "the scanner failed to flag a known-bad hardcoded reference"
    finally:
        probe_path.unlink()
