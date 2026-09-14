# Groq r3 export

This ZIP contains the r3 runner, tests, documentation, and local benchmark files.
It excludes credentials, virtual environments, caches, generated run outputs, and
shared ORFS dependency contents. It is not a standalone ORFS/toolchain installation.
Original program files are unchanged from the validated r3 version.

## Restore on Ubuntu/WSL

Use an existing OpenROAD-flow-scripts installation with its tools and RTL-OPT corpus.
Extract into a destination where flow/versions/phase5_to_9_groq_r3 does not exist:

```bash
cd /path/to/OpenROAD-flow-scripts/flow
mkdir -p versions
unzip /path/to/phase5_to_9_groq_r3_export.zip -d versions
cd versions/phase5_to_9_groq_r3
python3 restore_shared_links.py
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
chmod +x nebula.sh
./nebula.sh self-test
```

Python 3.12 matches the validated environment. The dependency lock is retained
from that environment; installation requires access to those package versions.
Shared links are recreated relative to the receiving flow directory. The restore
script does not overwrite existing files or copy tools, platforms, or the corpus.
Historical optional benchmark links can be absent; live RTL-OPT runs require the
corpus path and digest recorded in timing_automation/rtl_opt_corpus.json.

Provide GROQ_API_KEY in your shell or create a private .env.local before live runs.
No API key or reference to the original private configuration is included.

See OPERATING_COMMANDS.md for guided/exploratory/paired commands and offline reports.
See VALIDATION.md for implementation validation limits. EXPORT_MANIFEST.json gives
SHA-256 hashes for every payload file except itself. The ZIP is integrity-tested,
extracted, hash-verified, and its pure-Python self-test is run before delivery.
