# BHTTP/1 - HTTP, in binary

Course project (Network Architecture). Shreyansh Arora, 24bcs10252. Solo submission.

One protocol, two programs. The only thing that crosses between them is the spec (`SPEC.md`, also submitted as the two-page document).

- `bserve ROOT PORT`: the server (track 1). It accepts a connection, reads binary request frames, maps each path to a file under ROOT, and replies with status, headers and the bytes. Missing files get 404, malformed requests get 400, and the connection stays open. PORT 0 picks a free port. On startup it prints `bserve: listening on port N`. It listens on IPv6 and IPv4 at once where the host supports that, and on IPv4 otherwise.
- `bcurl [-v] [-I] host[:port][/path] [/path ...]`: the client (track 2). It builds the binary request, writes the body to stdout, and exits non-zero on 4xx/5xx. `-v` hexdumps every frame, sent and received, including error GOAWAYs, to stderr. `-I` sends HEAD. Every path goes over one connection. The port defaults to 9000 and the path to `/`; IPv6 literals go in brackets: `[::1]:9000/`.
- `bhttp.py`: the frame codec both programs use.
- `HEXDUMP.md`: the annotated hexdump deliverable. It is one real exchange: the real `bcurl -v localhost:9000/index.html` against a live bserve on port 9000, with every byte, the date included, taken from bcurl's own `-v` output. `HEXDUMP.txt` is the same dump without the notes. `make_hexdump.py` regenerates both.
- `interop/`: the interop evidence. See below.
- `tests/`: run with `python3 -m unittest discover -s tests -v`.

Python 3.9+ standard library only. Linux.

```
$ ./bserve ./www 9000 &
$ ./bcurl -v localhost:9000/index.html
```

**bcurl exit codes:**

| Code | Meaning |
|---|---|
| 0 | 1xx to 3xx |
| 4 | 4xx |
| 5 | 5xx |
| 1 | Connection or protocol error, timeout, or the server sent GOAWAY |
| 2 | Bad usage: arguments, port, a path too long for one frame, or a bad environment value |

**Tunables (environment).** Values are validated; a bad one exits 2.

| Variable | Default | What it controls |
|---|---|---|
| `BHTTP_FRAME_TIMEOUT` | 10 s | Time allowed to complete one frame once its first byte arrives |
| `BSERVE_IDLE_TIMEOUT` | 30 s | No new request since connect or the last response ended before the server closes with GOAWAY(0) |
| `BSERVE_MAX_CONNECTIONS` | 64 | Connection cap; a full server refuses new connections without GOAWAY, before reading the preface |
| `BCURL_TIMEOUT` | 30 s | Connect timeout, and the longest bcurl waits without receiving a frame |

## Design points worth checking

- **GOAWAY survives the close.** Every GOAWAY goes out through `goaway_close`: send, half-close (FIN), drain input for at most 1 s or 64 KiB, then close. A GOAWAY is not lost to a reset as long as the peer stops sending within 1 s / 64 KiB. Past that bound the close can still turn into an RST, by design, so a peer that keeps sending cannot hold the connection open. The old close-with-unread-input lost the GOAWAY 30 times out of 30 in a 30-run check. `tests/test_bhttp.py` asserts a clean EOF after every GOAWAY, including with 20 KB of unread payload pending and when a real HTTP/1 request arrives as the "preface".
- **No path TOCTOU.** `open_under_root` resolves the path, then opens it again one component at a time with `O_NOFOLLOW`, starting from a descriptor for the root. A symlink swapped in between the check and the open makes the walk fail. Size, type and mtime come from `fstat` on the open descriptor. A name too long for the file system is 404, like any other missing file.
- **The static table is ten names that are actually sent.** bcurl sends 1 to 3 and bserve sends 4 to 10. Index 8 was `connection`, which is meaningless on an always-persistent connection. It is now `etag`, which bserve sends together with `last-modified` and `cache-control`, and it backs `if-none-match` / 304.

## Tests

126 tests in three files (plus `tests/support.py` helpers). On a machine running as root without IPv6, 2 are skipped:
- the mode-000 file test, because root can read the file. Its 500 path is covered in-process, and the test passes when run as an unprivileged user.
- the dual-stack test, because the host has no IPv6.

- **`tests/test_bhttp.py`.** The real bserve over raw sockets:
  - Happy paths and boundaries: 40,000 bytes in 16384/16384/7232 frames, a 16384-byte file in one frame, a 16384-byte HEADERS payload.
  - HEAD, 304 (also on HEAD), empty file, `/` and directory index, literal `%2e%2e` (404), `//` and `.` segments, a trailing slash on a file (`/index.html/` 404, `/index.html/.` 200), an overlong path component (404, connection alive).
  - Fifteen kinds of malformed request, including an empty literal name and invalid UTF-8 in a header value. Each asserts **400 and then a 200 on the same connection**. Repeated and decreasing stream IDs.
  - Symlink escapes, unknown types (including one of exactly 16384 bytes), and every connection error, each asserting GOAWAY followed by a clean EOF.
  - Idle, slow-preface and slow-frame timeouts. A full server.
  - In-process server tests: 500 on OSError, GOAWAY(4) when a file shrinks mid-response, a quiet exit on peer reset, `respond()` validation, and the TOCTOU swap.
  - bcurl against scripted fake servers: exit codes; timeout (with GOAWAY(0)); every bad content-length (`١٢`, `²`, `+12`, ...), and 18 digits accepted while 19 are refused; over-length output stopped before the excess is written; HEAD or 304 HEADERS without END_STREAM refused at once; oversize frames (GOAWAY(2)); a server GOAWAY left unanswered; stray frames between responses (GOAWAY(1)) and after the last one (never written to stdout); `-v`. Several of these responses are built by hand, not with the codec.
  - In-process client tests over a socketpair, and codec tests: underrun at every truncation point, overrun, index bounds, name rules.
- **`tests/test_golden.py`.** Never imports the codec. It checks:
  - bcurl's bytes against hand-built bytes.
  - bcurl against the hard-coded golden response.
  - bserve's response **byte for byte** and every header value, parsed by hand. The date value at offset 0x19 is checked to be an IMF-fixdate and is the only 29 bytes masked.
  - that `HEXDUMP.txt` and `HEXDUMP.md` carry the same real exchange: the request equals the hand-built request for its `localhost:PORT` host, and the response equals the golden response with the same date mask. It also checks that the SPEC.md examples carry the golden frame headers.
- **`tests/test_interop.py`.** It covers:
  - the clean-room client against bserve;
  - bcurl against the clean-room server;
  - both clients through the fault-injecting proxy;
  - the clean-room client's own GOAWAY validation and timeout handling;
  - an AST check that pins both spec-only programs' imports.

## Interoperability, stated plainly

The brief asks for a partner who implements the other side from the spec alone. **I had no partner, and nothing in this folder replaces one.** Here is what exists, from weakest to strongest:

1. **Earlier reference pair** (`bhttp-reference.tar.gz`, 24 September). Written by me, separately, against the draft spec. Extracted as `./ref/` (`ref/bserve`, `ref/bcurl`), it joins the matrix automatically. It implements the superseded draft: index 8 is `connection`, not `etag`, and its responses differ in the ways listed below. It is the same author, so it shows consistency between my own programs, not that a stranger can build from the spec.
2. **`interop/cleanroom_client.py`.** A minimal client written from SPEC.md, with no shared code: it imports only `os`, `socket`, `struct` and `sys`, and it runs under `python3 -I`, so it cannot import `bhttp.py`. It carries its own static table and frame code. It is the same author, who had seen `bhttp.py`. It is spec-only code, not a clean room in the strict sense.
3. **`interop/cleanroom_server.py`.** The same idea for the other side, so bcurl has a peer that is not bserve. It imports only `os`, `socket`, `struct`, `sys` and `time` (for the date header) and runs under `python3 -I`. It has the same caveat as the clean-room client.
4. **`interop/his_client.py`.** A client implemented independently from SPEC.md alone, in a separate LLM session (a different model, not the one that wrote this project) that had no access to any file in this repository - not the server, not bcurl, not the tests. The submitter ran the session, tested the client against the reference server, and delivered it as a file; it is included byte-for-byte (sha256 in the manifest). `interop/his_shim.py` is harness plumbing only: it adapts the client's CLI to the matrix and cannot change its protocol behavior. This is the strongest evidence here: the spec alone was enough for an implementer with zero knowledge of this code.
5. **`interop/run.sh`.** A reproducible matrix: every server present against every client present. Each run goes through `interop/inject.py`, which records every byte and, in some scenarios, damages it:

   | Scenario | What it shows |
   |---|---|
   | GET 40,000 bytes, HEAD, 404 | Basic exchanges |
   | Three paths on one connection | The one-connection rule (proxy-counted) |
   | Unknown frames injected both ways | MUST-skip across implementations |
   | Flipped method byte | Server answers 400 |
   | Flipped preface | Server sends GOAWAY(3) |
   | Flipped response Length | Client sends GOAWAY(2) |
   | Truncated response | Client stops |
   | Slow link | Delays do not break the exchange |

   Transcripts (raw `.bin`, a hexdump and a log per run) go to `interop/transcripts/`. The script ends with the tally and the sha256 of every artifact.

**First complete run (2026-10-08).** This was before the clean-room server was added: servers mine and ref, clients bcurl, cleanroom and refcurl. Result: 51 PASS, 6 FAIL.

Every row with my server passed, including all of them with the reference client. All six failures were the reference server, against both bcurl and the clean-room client:

- **missing_404 and corrupt_method_400 (stdout differs).** The draft-era server sends a short body with its 404 and 400. The clients wrote that body to stdout and exited 4, as they should. The matrix expects an empty stdout because SPEC.md section 6 now says errors carry no body.
- **truncated_response (exit 0, want 1).** The draft-era response to `/index.html` is shorter than 140 bytes, so the proxy's cut lands after the response has ended. The clients correctly received a complete response.

These rows show the reference drifting from the current spec, not bcurl or the clean-room client failing it. `run.sh` now lists exactly these three (server, scenario) pairs as known draft-spec drift. A listed row that fails only in the documented way prints XFAIL with its reason. Any other failure, including a listed row failing some other way, is still FAIL.

**Final run of this tree (2026-10-08, night):** 138 passed, 18 xfail (documented drift), 4 n/a (not run), 0 failed, of 156 rows - servers bserve, cleanroom_server, the draft reference server and the independent his_server against clients bcurl, cleanroom_client, refcurl and the independent his_client. bcurl passed all ten scenarios against his_server, so both sides of the wire now have an independently written peer; his_server passed 38 of its 39 rows; the one XFAIL is the draft reference client sending no GOAWAY(2) on an oversize Length, not his_server. The four n/a rows are `head` with the independent client (it is GET-only); run.sh prints N/A and does not count them. The 18 xfail rows are all draft-reference drift: the draft server's error bodies, its GOAWAY(1) instead of GOAWAY(3) on a bad preface (now checked on the wire for every client, not just his_client), its response being too short for the truncation cut, and the draft client sending no GOAWAY(2) on an oversize response Length (also checked on the wire for every client). Every `his` error row also requires the scenario's signature line in the client's own log, and the one-connection check counts real proxy connections, so a passing row means what it says. Wire transcripts, per-row logs and the sha256 manifest are in `interop/transcripts/` and `interop/matrix_output.txt`.

One honest caveat about the independent client: it accepts a content-length of more than 18 ASCII digits, which SPEC.md section 6 forbids (receivers MUST reject any other value). It also exits 1 on a 404 instead of distinguishing statuses. Both are listed here rather than patched: the value of his_client is that someone else wrote it from the spec, and these are the two places a fresh reader slipped. Section 6 was tightened after this was found.

**What remains open:** the independent client and server were still produced with an LLM at the keyboard, by the submitter - not by an uninvolved classmate. They prove the spec is implementable from its text alone; they do not prove a disinterested second party finds it readable.

## Authorship and process

I am the sole author of this submission. I built it with AI assistance (Anthropic's Claude) for code, tests, spec wording, and repeated adversarial audits of the whole project. I made the design decisions and checked every result. Two pieces of interop evidence are independent of this project's code: `interop/his_client.py` (sha256 43c76a48...93aad) and `interop/his_server.py` (sha256 0fd516a8...a7cb) were each implemented from SPEC.md alone in separate LLM sessions that never saw this repository. The client session used a different model family from this project's assistant - it is the decorrelated reader; the server session used Claude Opus 5.5, the same family as the assistant, so it shares priors with the spec's wording and is the weaker reader of the two. The server session explicitly confirmed it had seen no bserve, bhttp.py or client code; its 38 passing checks were tests the session wrote for itself, not interop evidence. I ran both sessions and verified both programs against this tree before submitting them to the matrix. Feedback loop, stated plainly: no bserve, bcurl, test or matrix output was relayed into either session - the server session saw only SPEC.txt and worked on its own code (its transcript shows the whole exchange), and the client session's only contact with this project was my running its output against the reference server and this tree after it was written. The server session's transcript is at `interop/sessions/his_server-session.md`, trimmed to the cold-build evidence (the prompt, the spec-only constraint, the model's statements that it saw no existing code, and its 38-check test summary): local paths, code diffs and end-of-session troubleshooting were cut, everything kept is verbatim, and the fuller chat export it was trimmed from sits next to it as `his_server-session-fullexport.md` (sha256 461d5091...5fb4 vs 8838980f...c0f2 for the trimmed file; it is the session as exported from the chat UI, not the native Claude Code log - the session's own attempts to produce a verbatim export inside the chat failed, which the export's tail shows).

Spec version pinning: both sessions received the spec current on the afternoon of 2026-10-08 (his_server's session read it as SPEC.txt; the SPEC.md revision current that afternoon has sha256 ba5fba35...a8d9 and is preserved in this tree's history, and the submitted SPEC.md differs from it only by the evening edits listed here). The edits made that evening - the explicit receiver MUST on content-length, "errors carry content-length 0", the pipelined-request sentence, the section 3 stream-state wording and the idle-definition rewording - are changes neither session saw. his_client accepting a 19-digit content-length is consistent with that: the receiver rule then lived only in section 9's client error list, not next to the content-length definition in section 6.

For the viva: "how does a client cancel?" - it sends GOAWAY and reconnects; a cancel costs the connection (there is no RST_STREAM by design: one stream at a time, so a fresh connection is the cancel). Everything else here, including both spec-only programs, comes from one author working with one assistant.

## Wire check

The submitted spec PDF is `BHTTP-1-spec.pdf`, rendered from SPEC.md on A4 and verified at exactly 2 pages (`pdfinfo BHTTP-1-spec.pdf | grep Pages` prints `Pages: 2`) with: pandoc SPEC.md to HTML, then `wkhtmltopdf --disable-smart-shrinking --page-size A4` at 11pt and 20mm margins.

`make_hexdump.py` starts bserve on port 9000 (falling back to a free port, and saying so, if 9000 is busy). It gives `index.html` a fixed mtime, so last-modified and etag are stable. It then runs the real `bcurl -v` and rebuilds both byte streams from bcurl's own hexdump of every frame it sent and received.

Before writing, it checks:
- The sent bytes equal the codec-built preface, request and GOAWAY(0).
- The response equals the codec-built response with only the 29-byte live date masked.

Every annotation is derived from the bytes, with assertions:
- Length fields equal payload sizes.
- Value lengths equal value sizes.
- Indexes map through the SPEC table.
- END_STREAM sits where section 6 says.

SPEC.md is written to render on two pages at 11 pt (A4 or Letter, 20 mm margins); check it with pandoc and wkhtmltopdf after any edit.
