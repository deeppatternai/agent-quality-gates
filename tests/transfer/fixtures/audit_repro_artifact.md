# Sample artifact with known holes (Task 5 fixture)

This is a Transfer Test Pack v1 fixture deliberately crafted to elicit a
known set of audit findings. Used by Task 5 (audit reproducibility) to
verify that running `gpt_audit` against this artifact produces a
verdict containing at least the canonical findings declared in
`audit_repro_golden.yaml`.

The fixture is **frozen at v1.0.0**. Any intentional rotation must bump
`fixture_version` in `audit_repro_golden.yaml` and re-pin
`audit_repro_artifact_sha256`.

---

## Code under "review"

```python
import sqlite3
from flask import Flask, request

app = Flask(__name__)


@app.route("/search")
def search():
    q = request.args.get("q", "")
    conn = sqlite3.connect("app.db")
    cur = conn.cursor()
    # Direct string interpolation; no parameterization, no validation.
    cur.execute(f"SELECT * FROM items WHERE name LIKE '%{q}%'")
    rows = cur.fetchall()
    return {"rows": rows}


@app.route("/login", methods=["POST"])
def login():
    user = request.form.get("user", "")
    pw = request.form.get("pw", "")
    # No rate limit; brute force is unrestricted.
    if user == "admin" and pw == "hunter2":
        return {"token": "dev-token"}
    return {"error": "bad creds"}, 401


if __name__ == "__main__":
    app.run(debug=True)
```

## Known issues this fixture is designed to surface

The golden file expects an auditor to flag at minimum:

1. **Missing input validation on /search endpoint** — user input
   interpolated into SQL string with no validation or escaping.
2. **No rate limit on /login endpoint** — brute force unrestricted.

A v1 audit may also flag SQL injection, hardcoded credentials, or debug
mode left on; those are tolerated extras and do not block the golden
match.
